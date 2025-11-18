# Notion Backup Enhancer

Fixes various issues in Notion workspaces exports.

The fixed export files can be extracted into a directory and will allow for easy tracking of changes (e.g. using GitHub or another backup solution).

Simply drag and drop the Notion ZIP on the Notion Backup Enhancer executable:

![Drag and Drop Notion Zip](docs/drag-and-drop-zip.gif)

And a new ZIP file will be created: 

![Fixed ZIP File](docs/generated-files.png)

⚠️ Note this tool is EXPERIMENTAL - please validate the generated files. Any problems you encounter, please [raise an issue](https://github.com/pureleap/notion-backup-enhancer/issues)!

Based on [notion_export_enhancer](https://github.com/Cobertos/notion_export_enhancer).

## Features

- Takes a Notion export .zip file as input
- Removes trailing 32-hex Notion IDs from filenames and directories
- Fixes links pointing to renamed files
- Handles naming collisions by appending " (i)"
- Produces a new .zip file named `<input>.fixed.zip`

## How to Use

### Step 1: Create a Notion Workspace Export

1. Open your Notion workspace
2. Click on the workspace name in the top-left corner to open the workspace menu
3. Select "Settings & members" from the dropdown
4. In the Settings tab, scroll down to find the "Export" section
5. Click on "Export all workspace content"
6. Configure the export settings:
   - **Export format**: Markdown & CSV
   - **Include Databases**: Default View
   - **Include Content**: Everything
   - **Create Folders for Subpages**: Checked (enabled)
7. Click "Export" and wait for Notion to prepare your export file
8. Once ready, download the ZIP file to your computer

### Step 2: Download the Notion Backup Enhancer

- Go to the [Releases](https://github.com/pureleap/notion-backup-enhancer/releases) page
- Download the appropriate file for your operating system:
  - **Windows**: Download the `.exe` file
  - **Mac OS X**: Download the file without extension

### Step 3: Process the Export

- Drag and drop your Notion workspace export ZIP file onto the executable
- The tool will process the file and create:
  - A fixed ZIP file: `<your-export-filename>.fixed.zip` (in the same directory as your input file)
  - A log file: `<your-export-filename>.log.txt` (in the same directory as your input file, containing any errors or issues encountered)
- You will get a new ZIP file that has many of the issues with Notion exports resolved
