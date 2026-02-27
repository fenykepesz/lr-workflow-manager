@echo off
title Lightroom Workflow Manager
cd /d "%~dp0"
python lr_workflow.py %*
