import { isDownloadableUrl, openUrl } from './lib.js';

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'hotload-open',
    title: 'Open in Hotload',
    contexts: ['page', 'link', 'video', 'audio']
  });
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  const target = info.linkUrl || info.srcUrl || info.pageUrl;
  const { server } = await chrome.storage.local.get('server');
  if (!server) return chrome.runtime.openOptionsPage();
  if (!isDownloadableUrl(target)) return;
  chrome.tabs.create({ url: openUrl(server, target) });
});
