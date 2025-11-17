# Notion Backup Enhancer

Fixes various issues in Notion workspaces exports.

The fixed export files can be extracted into a directory and will allow for easy tracking of changes (e.g. using GitHub or another backup solution).

Based on [notion_export_enhancer](https://github.com/Cobertos/notion_export_enhancer).

## Features

- Takes a Notion export .zip file as input
- Removes trailing 32-hex Notion IDs from filenames and directories
- Fixes links pointing to renamed files
- Handles naming collisions by appending " (i)"
- Produces a new .zip file named `<input>.fixed.zip`

## How to Use

- Download Notion workspace export
- Run: `python export_fix.py path/to/notion-export.zip`
- The output will be `path/to/notion-export.fixed.zip`
