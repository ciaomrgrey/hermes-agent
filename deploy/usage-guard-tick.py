#!/usr/bin/env python3
"""Inactive no_agent cron entry; installation/release is owned by Hermes.

Native scheduler supplies the selected profile home and its Python interpreter.
Install the candidate into that interpreter; checkout PYTHONPATH is not guaranteed.
Configuration/state live outside the code checkout so upgrades preserve history.
"""
import sys
from hermes_constants import get_hermes_home
from usage_guard import main

if __name__ == '__main__':
    sys.argv = [__file__, 'sample', '--config', str(get_hermes_home()/'usage-guard'/'config.json')]
    main()
