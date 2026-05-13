/**
 * Context DNA Electron App
 *
 * This wraps the Next.js dashboard in a native desktop application.
 * It also manages the Context DNA API server lifecycle.
 *
 * Architecture:
 * - Electron main process manages windows and server
 * - Loads the Next.js dashboard (either local dev or built)
 * - Can optionally start/stop the API server
 * - Provides native OS integration (tray, notifications, shortcuts)
 */

const { app, BrowserWindow, Tray, Menu, nativeImage, shell, ipcMain, Notification } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const Store = require('electron-store');

// Persistent settings
const store = new Store({
  defaults: {
    apiUrl: 'http://127.0.0.1:3456',
    dashboardUrl: 'http://localhost:3457',
    startServerOnLaunch: true,
    showInTray: true,
    startMinimized: false,
  },
});

let mainWindow = null;
let tray = null;
let serverProcess = null;

const isDev = process.argv.includes('--dev');

/**
 * Create the main application window
 */
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: 'Context DNA',
    icon: path.join(__dirname, 'assets', 'icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
    titleBarStyle: 'hiddenInset', // macOS nice title bar
    show: !store.get('startMinimized'),
  });

  // Load the dashboard
  const dashboardUrl = store.get('dashboardUrl');
  mainWindow.loadURL(dashboardUrl).catch((err) => {
    console.error('Failed to load dashboard:', err);
    // Show error page if dashboard not running
    mainWindow.loadFile(path.join(__dirname, 'renderer', 'error.html'));
  });

  // Handle external links
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Hide instead of close when clicking X
  mainWindow.on('close', (event) => {
    if (!app.isQuitting && store.get('showInTray')) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Open DevTools in dev mode
  if (isDev) {
    mainWindow.webContents.openDevTools();
  }
}

/**
 * Create system tray icon
 */
function createTray() {
  // Create tray icon (16x16 for macOS, 32x32 for others)
  const iconPath = path.join(__dirname, 'assets', 'tray-icon.png');
  const icon = nativeImage.createFromPath(iconPath);

  tray = new Tray(icon.resize({ width: 16, height: 16 }));
  tray.setToolTip('Context DNA');

  updateTrayMenu();

  // Show window on tray click
  tray.on('click', () => {
    if (mainWindow) {
      if (mainWindow.isVisible()) {
        mainWindow.focus();
      } else {
        mainWindow.show();
      }
    }
  });
}

/**
 * Update tray menu with current stats
 */
async function updateTrayMenu() {
  let statsText = 'Loading...';

  try {
    const response = await fetch(`${store.get('apiUrl')}/api/stats`);
    if (response.ok) {
      const stats = await response.json();
      statsText = `🧠 ${stats.total} | 🏆 ${stats.wins} | 🔥 ${stats.streak}`;
    } else {
      statsText = 'Server not running';
    }
  } catch (error) {
    statsText = 'Server not running';
  }

  const contextMenu = Menu.buildFromTemplate([
    { label: 'Context DNA', enabled: false },
    { type: 'separator' },
    { label: statsText, enabled: false },
    { type: 'separator' },
    {
      label: 'Record Win',
      accelerator: 'CmdOrCtrl+Shift+W',
      click: () => recordQuick('win'),
    },
    {
      label: 'Record Fix',
      accelerator: 'CmdOrCtrl+Shift+F',
      click: () => recordQuick('fix'),
    },
    { type: 'separator' },
    {
      label: 'Open Dashboard',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        } else {
          createWindow();
        }
      },
    },
    {
      label: 'Open in Browser',
      click: () => shell.openExternal(store.get('dashboardUrl')),
    },
    { type: 'separator' },
    {
      label: 'Server',
      submenu: [
        {
          label: 'Start Server',
          click: startServer,
          enabled: !serverProcess,
        },
        {
          label: 'Stop Server',
          click: stopServer,
          enabled: !!serverProcess,
        },
        {
          label: 'Restart Server',
          click: () => {
            stopServer();
            setTimeout(startServer, 1000);
          },
        },
      ],
    },
    { type: 'separator' },
    {
      label: 'Quit',
      accelerator: 'CmdOrCtrl+Q',
      click: () => {
        app.isQuitting = true;
        app.quit();
      },
    },
  ]);

  tray.setContextMenu(contextMenu);
}

/**
 * Quick record from tray
 */
async function recordQuick(type) {
  const { dialog } = require('electron');

  const result = await dialog.showMessageBox(mainWindow, {
    type: 'question',
    buttons: ['Cancel', 'Record'],
    defaultId: 1,
    title: `Record ${type === 'win' ? 'Win' : 'Fix'}`,
    message: `What ${type === 'win' ? 'worked' : 'did you fix'}?`,
    detail: 'Enter a title for your learning',
  });

  // Note: This is a simplified version. In production, use a custom dialog
  // or show the main window with the quick add form focused.
  if (result.response === 1) {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.webContents.executeJavaScript(`
        // Focus the quick add form
        document.querySelector('input[placeholder*="${type === 'win' ? 'worked' : 'problem'}"]')?.focus();
      `);
    }
  }
}

/**
 * Start the Context DNA API server
 */
function startServer() {
  if (serverProcess) {
    console.log('Server already running');
    return;
  }

  console.log('Starting Context DNA server...');

  serverProcess = spawn('context-dna', ['serve'], {
    shell: true,
    stdio: 'pipe',
  });

  serverProcess.stdout.on('data', (data) => {
    console.log(`Server: ${data}`);
  });

  serverProcess.stderr.on('data', (data) => {
    console.error(`Server error: ${data}`);
  });

  serverProcess.on('close', (code) => {
    console.log(`Server exited with code ${code}`);
    serverProcess = null;
    updateTrayMenu();
  });

  // Update tray after server starts
  setTimeout(updateTrayMenu, 2000);

  // Show notification
  new Notification({
    title: 'Context DNA',
    body: 'Server started',
  }).show();
}

/**
 * Stop the Context DNA API server
 */
function stopServer() {
  if (serverProcess) {
    console.log('Stopping Context DNA server...');
    serverProcess.kill();
    serverProcess = null;
    updateTrayMenu();
  }
}

/**
 * App lifecycle
 */
app.whenReady().then(() => {
  // Start server if configured
  if (store.get('startServerOnLaunch')) {
    startServer();
  }

  // Create window
  createWindow();

  // Create tray
  if (store.get('showInTray')) {
    createTray();
  }

  // macOS: re-create window when dock icon is clicked
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    } else if (mainWindow) {
      mainWindow.show();
    }
  });

  // Refresh tray stats periodically
  setInterval(updateTrayMenu, 30000);
});

app.on('window-all-closed', () => {
  // Don't quit on macOS when all windows closed (stay in tray)
  if (process.platform !== 'darwin' || !store.get('showInTray')) {
    app.quit();
  }
});

app.on('before-quit', () => {
  app.isQuitting = true;
  stopServer();
});

// IPC handlers for renderer
ipcMain.handle('get-settings', () => store.store);
ipcMain.handle('set-setting', (event, key, value) => store.set(key, value));
ipcMain.handle('get-server-status', () => !!serverProcess);
