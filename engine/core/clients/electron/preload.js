/**
 * Preload script for Context DNA Electron app
 *
 * Exposes safe APIs to the renderer process
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('contextDNA', {
  // Settings
  getSettings: () => ipcRenderer.invoke('get-settings'),
  setSetting: (key, value) => ipcRenderer.invoke('set-setting', key, value),

  // Server status
  getServerStatus: () => ipcRenderer.invoke('get-server-status'),

  // App info
  platform: process.platform,
  version: process.env.npm_package_version || '0.1.0',
});
