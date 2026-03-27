#!/bin/bash
gh release download --pattern "NotionBackupEnhancer-ubuntu" --clobber
chmod +x NotionBackupEnhancer-ubuntu
echo "Downloaded and made executable: NotionBackupEnhancer-ubuntu"