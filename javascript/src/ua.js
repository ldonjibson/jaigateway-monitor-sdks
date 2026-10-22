/** Small user-agent parser: enough to say "Chrome 126" and "macOS" on an error. */
export function parseUA(ua) {
  ua = String(ua || '');
  let browser = 'Unknown';
  let m;
  if ((m = /(Edg|EdgA|EdgiOS)\/([\d.]+)/.exec(ua))) browser = `Edge ${m[2].split('.')[0]}`;
  else if ((m = /(OPR|Opera)\/([\d.]+)/.exec(ua))) browser = `Opera ${m[2].split('.')[0]}`;
  else if ((m = /(Firefox|FxiOS)\/([\d.]+)/.exec(ua))) browser = `Firefox ${m[2].split('.')[0]}`;
  else if ((m = /(Chrome|CriOS)\/([\d.]+)/.exec(ua))) browser = `Chrome ${m[2].split('.')[0]}`;
  else if ((m = /Version\/([\d.]+).*Safari/.exec(ua))) browser = `Safari ${m[1].split('.')[0]}`;
  else if (/MSIE |Trident\//.test(ua)) browser = 'Internet Explorer';
  let os = 'Unknown';
  if (/Windows/.test(ua)) os = 'Windows';
  else if (/Android/.test(ua)) os = 'Android';
  else if (/iPhone|iPad|iPod/.test(ua)) os = 'iOS';
  else if (/Mac OS X|Macintosh/.test(ua)) os = 'macOS';
  else if (/CrOS/.test(ua)) os = 'ChromeOS';
  else if (/Linux/.test(ua)) os = 'Linux';
  return { browser, os };
}
