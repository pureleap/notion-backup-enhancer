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
- Download the executable from the [Releases](https://github.com/pureleap/notion-backup-enhancer/releases) for your operating system 
- Drag and drop the Notion workspace export ZIP onto the executable
- You will get a new ZIP file that has many of the issues with Notion exports resolved