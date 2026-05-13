#!/usr/bin/env node
/**
 * Google Drive Discovery Script
 * Searches for ER Simulator-related files and builds inventory
 */

const { google } = require('googleapis');
const fs = require('fs');
const path = require('path');
require('dotenv').config();

const TOKEN_PATH = path.join(__dirname, '..', 'simulator-core', 'er-sim-monitor', 'config', 'token.json');

// Search keywords for ER Simulator project
const SEARCH_KEYWORDS = [
  'ER Simulator',
  'er-sim',
  'sim-monitor',
  'vitals',
  'monitor',
  'sim case',
  'scenario',
  'Sim Builder',
  'Convert_Master_Sim',
  'ATSR',
  'Apps Script',
  'expo',
  'react native'
];

// File extensions to look for
const CODE_EXTENSIONS = ['.gs', '.js', '.ts', '.jsx', '.tsx', '.json', '.html'];
const CSV_EXTENSIONS = ['.csv'];
const DOC_EXTENSIONS = ['.md', '.txt', '.pdf', '.docx'];

async function authenticateGoogleDrive() {
  const credentials = {
    installed: {
      client_id: process.env.GOOGLE_CLIENT_ID,
      client_secret: process.env.GOOGLE_CLIENT_SECRET,
      redirect_uris: ['http://localhost']
    }
  };

  const { client_secret, client_id, redirect_uris } = credentials.installed;
  const oAuth2Client = new google.auth.OAuth2(client_id, client_secret, redirect_uris[0]);

  if (!fs.existsSync(TOKEN_PATH)) {
    console.error('❌ Token file not found at:', TOKEN_PATH);
    process.exit(1);
  }

  const token = JSON.parse(fs.readFileSync(TOKEN_PATH, 'utf-8'));
  oAuth2Client.setCredentials(token);

  return oAuth2Client;
}

function classifyFile(file) {
  const name = file.name.toLowerCase();
  const mimeType = file.mimeType;

  // Classify by type
  let category = 'other';
  let subcategory = 'unknown';
  let confidence = 'low';

  // Check if it's a folder
  if (mimeType === 'application/vnd.google-apps.folder') {
    category = 'folder';
    confidence = 'high';
  }
  // Check for code files
  else if (CODE_EXTENSIONS.some(ext => name.endsWith(ext)) ||
           mimeType === 'application/vnd.google-apps.script') {
    category = 'code';

    // Further classify code
    if (name.includes('atsr') || name.includes('spark') || name.includes('memory')) {
      subcategory = 'apps-script-tools';
      confidence = 'high';
    } else if (name.includes('sim') || name.includes('builder') || name.includes('convert')) {
      subcategory = 'sim-builder';
      confidence = 'high';
    } else if (name.includes('.gs')) {
      subcategory = 'apps-script';
      confidence = 'medium';
    } else {
      subcategory = 'general-code';
      confidence = 'medium';
    }
  }
  // Check for CSV files
  else if (CSV_EXTENSIONS.some(ext => name.endsWith(ext)) ||
           mimeType === 'text/csv') {
    category = 'csv';

    if (name.includes('template') || name.includes('master') || name.includes('input')) {
      subcategory = 'raw';
      confidence = 'high';
    } else if (name.includes('output') || name.includes('clean') || name.includes('processed')) {
      subcategory = 'clean';
      confidence = 'high';
    } else {
      subcategory = 'unknown-csv';
      confidence = 'medium';
    }
  }
  // Check for documentation
  else if (DOC_EXTENSIONS.some(ext => name.endsWith(ext)) ||
           mimeType.includes('document') ||
           mimeType.includes('text/plain')) {
    category = 'docs';
    subcategory = 'documentation';
    confidence = 'high';
  }
  // Google Sheets
  else if (mimeType === 'application/vnd.google-apps.spreadsheet') {
    category = 'sheets';
    if (name.includes('convert') || name.includes('master') || name.includes('sim')) {
      subcategory = 'scenario-data';
      confidence = 'high';
    } else {
      subcategory = 'spreadsheet';
      confidence = 'medium';
    }
  }

  return { category, subcategory, confidence };
}

function isRelevant(file) {
  const name = file.name.toLowerCase();

  // Check if filename matches any keywords
  const keywordMatch = SEARCH_KEYWORDS.some(keyword =>
    name.includes(keyword.toLowerCase())
  );

  // Check file type relevance
  const typeMatch =
    CODE_EXTENSIONS.some(ext => name.endsWith(ext)) ||
    CSV_EXTENSIONS.some(ext => name.endsWith(ext)) ||
    DOC_EXTENSIONS.some(ext => name.endsWith(ext)) ||
    file.mimeType === 'application/vnd.google-apps.script' ||
    file.mimeType === 'application/vnd.google-apps.spreadsheet' ||
    file.mimeType === 'application/vnd.google-apps.folder';

  return keywordMatch || typeMatch;
}

async function searchDriveFiles(drive) {
  console.log('🔍 Searching Google Drive for ER Simulator files...\n');

  const inventory = {
    code: [],
    csv: [],
    docs: [],
    sheets: [],
    folders: [],
    other: []
  };

  let pageToken = null;
  let totalFiles = 0;

  do {
    const response = await drive.files.list({
      pageSize: 100,
      fields: 'nextPageToken, files(id, name, mimeType, createdTime, modifiedTime, size, parents)',
      pageToken: pageToken,
      q: "trashed=false"
    });

    const files = response.data.files;
    totalFiles += files.length;

    for (const file of files) {
      if (isRelevant(file)) {
        const classification = classifyFile(file);

        const fileInfo = {
          id: file.id,
          name: file.name,
          mimeType: file.mimeType,
          category: classification.category,
          subcategory: classification.subcategory,
          confidence: classification.confidence,
          modifiedTime: file.modifiedTime,
          size: file.size || 'N/A',
          parents: file.parents || []
        };

        // Add to appropriate category
        if (classification.category === 'code') {
          inventory.code.push(fileInfo);
        } else if (classification.category === 'csv') {
          inventory.csv.push(fileInfo);
        } else if (classification.category === 'docs') {
          inventory.docs.push(fileInfo);
        } else if (classification.category === 'sheets') {
          inventory.sheets.push(fileInfo);
        } else if (classification.category === 'folder') {
          inventory.folders.push(fileInfo);
        } else {
          inventory.other.push(fileInfo);
        }
      }
    }

    pageToken = response.data.nextPageToken;
  } while (pageToken);

  console.log(`✅ Scanned ${totalFiles} total files in Drive\n`);

  return inventory;
}

async function main() {
  try {
    const auth = await authenticateGoogleDrive();
    const drive = google.drive({ version: 'v3', auth });

    const inventory = await searchDriveFiles(drive);

    // Print summary
    console.log('📊 DISCOVERY SUMMARY:\n');
    console.log(`📁 Folders: ${inventory.folders.length}`);
    console.log(`💾 Code files: ${inventory.code.length}`);
    console.log(`📊 Sheets: ${inventory.sheets.length}`);
    console.log(`📄 CSV files: ${inventory.csv.length}`);
    console.log(`📝 Documentation: ${inventory.docs.length}`);
    console.log(`❓ Other: ${inventory.other.length}`);
    console.log(`\n📦 Total relevant files: ${
      inventory.folders.length +
      inventory.code.length +
      inventory.sheets.length +
      inventory.csv.length +
      inventory.docs.length +
      inventory.other.length
    }\n`);

    // Save inventory to JSON
    const outputPath = path.join(__dirname, '..', 'tmp', 'drive-inventory.json');
    fs.writeFileSync(outputPath, JSON.stringify(inventory, null, 2));
    console.log(`💾 Inventory saved to: ${outputPath}\n`);

    // Print details by category
    if (inventory.code.length > 0) {
      console.log('\n💾 CODE FILES:');
      inventory.code.forEach(file => {
        console.log(`  - ${file.name} (${file.subcategory}, confidence: ${file.confidence})`);
      });
    }

    if (inventory.sheets.length > 0) {
      console.log('\n📊 GOOGLE SHEETS:');
      inventory.sheets.forEach(file => {
        console.log(`  - ${file.name} (${file.subcategory}, confidence: ${file.confidence})`);
      });
    }

    if (inventory.csv.length > 0) {
      console.log('\n📄 CSV FILES:');
      inventory.csv.forEach(file => {
        console.log(`  - ${file.name} (${file.subcategory}, confidence: ${file.confidence})`);
      });
    }

    if (inventory.docs.length > 0) {
      console.log('\n📝 DOCUMENTATION:');
      inventory.docs.forEach(file => {
        console.log(`  - ${file.name} (${file.subcategory}, confidence: ${file.confidence})`);
      });
    }

    if (inventory.folders.length > 0) {
      console.log('\n📁 RELEVANT FOLDERS:');
      inventory.folders.forEach(file => {
        console.log(`  - ${file.name}`);
      });
    }

    if (inventory.other.length > 0) {
      console.log('\n❓ OTHER FILES:');
      inventory.other.forEach(file => {
        console.log(`  - ${file.name} (${file.mimeType})`);
      });
    }

  } catch (error) {
    console.error('❌ Error:', error.message);
    process.exit(1);
  }
}

main();
