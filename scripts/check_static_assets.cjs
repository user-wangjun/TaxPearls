/* 静态资源契约：index.html 引用的每个自有资源，都必须在 webapp/app.py 注册路由。

   为什么需要这条检查：本项目的静态资源走「逐文件白名单」——app.py 里没有
   StaticFiles 挂载，每个 .js/.css 都要有一个 @app.get("/xxx.js") 返回 FileResponse。
   只在 index.html 里加 <script src="/new.js"> 而不注册路由，页面会静默 404，
   浏览器只报一句 "Failed to load resource"，功能看起来像"没实现"。

   同时校验文件真实存在，避免删了文件却留着引用。 */
'use strict';
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'webapp', 'static', 'index.html'), 'utf8');
// 路由散落在各模块（app.py 只注册一部分），所以要扫整个包的 python 源码
const sources = [path.join(root, 'main.py')];
for (const entry of fs.readdirSync(path.join(root, 'webapp'))) {
  if (entry.endsWith('.py')) sources.push(path.join(root, 'webapp', entry));
}
let pySource = '';
for (const file of sources) pySource += fs.readFileSync(file, 'utf8') + '\n';

const registered = new Set();
for (const match of pySource.matchAll(/@app\.(?:get|post)\("\/([^"]+)"/g)) registered.add(match[1]);
for (const match of pySource.matchAll(/"\/([^"]+)":\s*\("/g)) registered.add(match[1]);

function fail(message) {
  console.error('FAIL ' + message);
  process.exitCode = 1;
}

// index.html 里引用的自有资源：src/href 以 / 开头、不带协议与域名
const refs = new Set();
for (const match of html.matchAll(/<(?:script|link)[^>]+(?:src|href)="\/([^"]+?)(\?[^"]*)?"/g)) {
  const name = match[1];
  if (/^[a-z]+:\/\//.test(name)) continue;      // 外链跳过
  if (!/\.[a-z0-9]+$/i.test(name)) continue;    // 无扩展名的是动态接口（如 /logo），不算静态资源
  refs.add(name);
}

const missingRoute = [];
const missingFile = [];
for (const name of [...refs].sort()) {
  if (!registered.has(name)) missingRoute.push(name);
  if (!fs.existsSync(path.join(root, 'webapp', 'static', name))) missingFile.push(name);
}

if (missingRoute.length) {
  fail('index.html 引用了未在 app.py 注册路由的资源（页面会静默 404）：'
    + missingRoute.join('、'));
}
if (missingFile.length) {
  fail('index.html 引用了 static 目录下不存在的资源：' + missingFile.join('、'));
}

if (!process.exitCode) {
  console.log(`Static assets: ${refs.size} 个自有引用全部有路由、文件也都在。`);
}
