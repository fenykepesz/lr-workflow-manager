#!/bin/bash
# Syncs Lightroom presets between this SSD and the Mac, in both directions.
# Placed here by Lightroom Workflow Manager at Leave.
#
# Run it after connecting the SSD, and again before ejecting it. Presets are
# matched by path, content and ID, with the same rules as lrwm/presets.py.
# Replaced files are backed up, nothing is deleted. Works with the bash 3.2 and
# the perl that come with macOS.
set -u

SSD="$(cd "$(dirname "$0")" && pwd)" || exit 1
MAC="${LRWM_ADOBE_DIR:-$HOME/Library/Application Support/Adobe}"
STATE="${LRWM_STATE_DIR:-$HOME/Library/Application Support/LRWM}"

finish() {
  if [ -t 0 ]; then
    printf '\nPress Return to close. '
    read -r _
  fi
  exit "$1"
}

offer_eject() {
  local volume answer
  volume="$(dirname "$SSD")"
  [ -t 0 ] || exit 0  # not interactive
  case "$volume" in
    /Volumes/?*) ;;
    *) finish 0 ;;
  esac
  printf '\nFinished with the SSD? Type y and Return to eject it, or just Return to keep working. '
  read -r answer
  case "$answer" in
    y|Y|yes|YES|Yes) ;;
    *) exit 0 ;;
  esac
  if pgrep -f "Adobe Lightroom Classic.app/Contents/MacOS/Adobe Lightroom Classic" >/dev/null; then
    echo "Lightroom Classic is open. Quit it, then run this again to eject."
    exit 1
  fi
  # bash holds this script open on the SSD, so hand over to a shell that doesn't.
  cd /
  exec /bin/bash -c '
    sleep 1
    if diskutil eject "$1"; then
      echo "Ejected. You can unplug the SSD now."
    else
      echo "Could not eject: the SSD is still in use. Do not unplug it yet."
      echo "Quit Photoshop and anything else using it, then eject it in Finder."
      exit 1
    fi' eject-ssd "$volume"
}

if [ -z "${LRWM_ADOBE_DIR:-}" ]; then
  if [ "$(uname)" != "Darwin" ]; then
    echo "This script is for the Mac. On the PC, use Leave and Arrive."
    finish 1
  fi
  if pgrep -f "Adobe Lightroom Classic.app/Contents/MacOS/Adobe Lightroom Classic" >/dev/null; then
    echo "Lightroom Classic is running. Quit it, then run this again."
    finish 1
  fi
fi

if ! command -v perl >/dev/null; then
  echo "This Mac has no perl, which the preset sync needs. Presets were not synced."
  finish 1
fi
mkdir -p "$STATE" || finish 1

IFS= read -r -d '' SYNC <<'PERL'
use strict;
use warnings;
use Digest::MD5 qw(md5_hex);
use Encode qw(decode encode);
use File::Basename qw(basename dirname);
use File::Copy ();
use File::Path qw(make_path);
use POSIX qw(strftime);
use Unicode::Normalize qw(NFC);

$| = 1;
my ($ssd, $mac, $state) = @ARGV;
my @folders = ('CameraRaw/Settings', 'CameraRaw/ImportedSettings', 'Lightroom/Local Adjustment Presets');
my $tolerance = 2;
my $seen_file = "$state/presets-seen.txt";
my $marker = "$state/last-sync";
my $backup = "$state/PresetBackups/" . strftime('%Y-%m-%d_%H%M%S', localtime);
my $last = -e $marker ? (stat $marker)[9] : undef;
my $failed = 0;

sub key_of { return encode('UTF-8', lc NFC(decode('UTF-8', $_[0]))) }

sub scan {
    my ($root) = @_;
    my %found;
    my @todo = grep { -d "$root/$_" } @folders;
    while (defined(my $dir = shift @todo)) {
        my $handle;
        if (!opendir $handle, "$root/$dir") {
            print "  Could not read $root/$dir\n";
            $failed++;
            next;
        }
        for my $name (readdir $handle) {
            next if $name eq '.' or $name eq '..' or $name =~ /^\._/;
            my $rel = "$dir/$name";
            my $path = "$root/$rel";
            next if -l $path;
            if (-d $path) { push @todo, $rel; next }
            next unless $name =~ /\.(xmp|lrtemplate)\z/i;
            my $type = lc $1;
            next if $type eq 'lrtemplate' and $name =~ /^~/;    # left behind by the 7.3 conversion
            my $in;
            if (!open $in, '<', $path) {
                print "  Could not read $path\n";
                $failed++;
                next;
            }
            binmode $in;
            my $body = do { local $/; <$in> };
            $body = '' unless defined $body;
            my $mtime = (stat $in)[9];
            close $in;
            $body =~ s/\r\n/\n/g;
            $body =~ s/\s+\z//;
            my ($id) = $type eq 'xmp' ? $body =~ /crs:UUID="([^"]+)"/ : $body =~ /^\s*id = "([^"]+)"/m;
            $found{key_of($rel)} = { rel => $rel, mtime => $mtime, digest => md5_hex($body),
                                     id => defined $id ? uc $id : undef };
        }
        closedir $handle;
    }
    return \%found;
}

sub copy_file {
    my ($from, $to) = @_;
    my $tmp = "$to.lrwm-tmp";
    my $ok = eval {
        make_path(dirname($to));
        File::Copy::copy($from, $tmp) or die "copy: $!\n";
        my $mtime = (stat $from)[9];
        utime $mtime, $mtime, $tmp;
        rename $tmp, $to or die "rename: $!\n";
        1;
    };
    unless ($ok) {
        print "  Could not copy $from\n";
        unlink $tmp;
        $failed++;
    }
    return $ok;
}

my %seen;
if (open my $in, '<', $seen_file) {
    while (my $line = <$in>) {
        chomp $line;
        $seen{$line =~ /^[#@]/ ? $line : key_of($line)} = 1 if length $line;
    }
    close $in;
}

sub sync_one_way {
    my ($from, $to, $name) = @_;
    my ($src, $dst) = (scan($from), scan($to));
    my (%digests, %by_id, %copies);
    for my $p (values %$dst) {
        $digests{$p->{digest}} = 1;
        push @{ $by_id{$p->{id}} }, $p if defined $p->{id};
    }
    my ($elsewhere, $versions, $kept, $deleted) = (0, 0, 0, 0);
    my $plan = sub {
        my ($p, $rel, $replaces) = @_;
        my $k = key_of($rel);
        $copies{$k} = [$p, $rel, $replaces] if !$copies{$k} or $p->{mtime} > $copies{$k}[0]{mtime};
    };
    for my $k (sort keys %$src) {
        my $p = $src->{$k};
        my $id = $p->{id};
        if (my $old = $dst->{$k}) {
            next if $p->{digest} eq $old->{digest};
            if ($p->{mtime} > $old->{mtime} + $tolerance) { $plan->($p, $old->{rel}, 1) }
            else { $kept++ }
        } elsif ($digests{$p->{digest}}) {
            $elsewhere++;
        } elsif (defined $id and $by_id{$id}) {
            my @others = @{ $by_id{$id} };
            my %different = map { $_->{digest} => 1 } @others;
            my $older = grep { $p->{mtime} > $_->{mtime} + $tolerance } @others;
            if (defined $last and $p->{mtime} > $last and $older == @others and keys(%different) == 1) {
                $plan->($p, $_->{rel}, 1) for @others;
            } else {
                $versions++;
            }
        } elsif ($name eq 'Mac' and %$dst and
                 ($seen{"#$p->{digest}"} or
                  (($seen{$k} or (defined $id and $seen{"\@$id"})) and defined $last and $p->{mtime} <= $last))) {
            $deleted++;
        } else {
            $plan->($p, $p->{rel}, 0);
        }
    }
    my ($new, $updated) = (0, 0);
    for my $k (sort keys %copies) {
        my ($p, $rel, $replaces) = @{ $copies{$k} };
        next if $replaces and !copy_file("$to/$rel", "$backup/$name/$rel");
        next unless copy_file("$from/$p->{rel}", "$to/$rel");
        $replaces ? $updated++ : $new++;
    }
    print "To $name: $new new, $updated updated, $elsewhere already there in another folder, ",
          "$versions older variants left alone, $kept newer there, $deleted deleted there (not restored)\n";
}

sync_one_way($ssd, $mac, 'Mac');
sync_one_way($mac, $ssd, 'SSD');

my $have = scan($mac);
for my $k (keys %$have) {
    $seen{$k} = 1;
    $seen{"#$have->{$k}{digest}"} = 1;
    $seen{"\@$have->{$k}{id}"} = 1 if defined $have->{$k}{id};
}
if (open my $out, '>', "$seen_file.tmp") {
    print {$out} "$_\n" for sort keys %seen;
    if (close $out) { rename "$seen_file.tmp", $seen_file or $failed++ }
    else { $failed++ }
} else {
    $failed++;
}

if ($failed) {
    print "Finished with $failed error(s). Fix them and run this again.\n";
    exit 1;
}
open my $touch, '>', $marker or die "Could not write $marker: $!\n";
close $touch;
PERL

echo "Syncing Lightroom presets..."
perl -e "$SYNC" "$SSD" "$MAC" "$STATE" || finish 1
echo "Done. Nik presets are not included: use export and import inside Nik."
offer_eject
