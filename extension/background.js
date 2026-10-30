import { isDownloadableUrl, openUrl } from './lib.js';
import { ext } from './compat.js';

ext.runtime.onInstalled.addListener(() => {
  ext.contextMenus.create({
    id: 'hotload-open',
    title: 'Open in Hotload',
    contexts: ['page', 'link', 'video', 'audio']
  });
});

ext.contextMenus.onClicked.addListener(async (info) => {
  const target = info.linkUrl || info.srcUrl || info.pageUrl;
  const { server } = await ext.storage.local.get('server');
  if (!server) return ext.runtime.openOptionsPage();
  if (!isDownloadableUrl(target)) return;
  ext.tabs.create({ url: openUrl(server, target) });
});
