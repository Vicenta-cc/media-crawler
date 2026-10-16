/* Browser-native blocking runs before network redirects and popup documents. */
let blocked = 0;
let headersReady = false;
const redirected = new Set();
const allowed = (details) => {
  try {
    const u = new URL(details.url);
    if (details.type !== "main_frame" && details.type !== "sub_frame") return {};
    const dy = u.protocol === "https:" && (u.hostname === "douyin.com" || u.hostname.endsWith(".douyin.com"));
    const iframe = details.type === "sub_frame" && u.protocol === "https:" && u.hostname === "lf-rc1.yhgfb-cn-static.com";
    const echo = details.type === "main_frame" && details.method === "GET" && details.url === ZHANYUN_POLICY.echoUrl && !redirected.has(details.requestId);
    if (dy || iframe || echo) return {};
  } catch (_) {}
  blocked += 1;
  return {cancel: true};
};
browser.webRequest.onBeforeRequest.addListener(allowed, {urls:["<all_urls>"],types:["main_frame","sub_frame"]}, ["blocking"]);
browser.runtime.onMessage.addListener((message, sender) => {
  if (sender.id === browser.runtime.id && message && message.type === "policy-ready")
    return Promise.resolve({ready:true,headersReady,version:1,blocked});
});

const documentFilter = {urls:["<all_urls>"],types:["main_frame","sub_frame"]};
browser.webRequest.onBeforeRedirect.addListener(details => redirected.add(details.requestId), documentFilter);
browser.webRequest.onCompleted.addListener(details => redirected.delete(details.requestId), documentFilter);
browser.webRequest.onErrorOccurred.addListener(details => redirected.delete(details.requestId), documentFilter);

// Strip only origin credentials on the exact echo request. Unlike replacing
// Playwright request headers this preserves Firefox's native proxy challenge.
browser.webRequest.onBeforeSendHeaders.addListener(details => {
  if (details.url !== ZHANYUN_POLICY.echoUrl) return {};
  return {requestHeaders:(details.requestHeaders || []).filter(header =>
    !["cookie", "authorization"].includes(header.name.toLowerCase()))};
}, {urls:["<all_urls>"]}, ["blocking", "requestHeaders"]);

headersReady = true;
