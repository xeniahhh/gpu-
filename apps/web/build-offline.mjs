/**
 * Offline fallback for a machine with JS packages but no usable Vite native
 * dependencies. Run `node build-offline.mjs` from any directory.
 * Keep `pnpm install && pnpm build` as the normal build path. This fallback
 * transpiles without type checking; run TypeScript checks separately.
 * It bundles the installed React production modules without network access.
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';

const root = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const ts = require('typescript');
const packageDir = (name) => path.dirname(require.resolve(`${name}/package.json`));
const react = packageDir('react');
const reactDom = packageDir('react-dom');
let scheduler;
try {
  scheduler = path.dirname(createRequire(path.join(reactDom, 'package.json')).resolve('scheduler/package.json'));
} catch {
  // pnpm packages copied from another operating system may lose peer links.
  scheduler = path.join(root, 'node_modules/.pnpm/node_modules/scheduler');
}
const read = (file) => fs.readFileSync(file, 'utf8');
const modules = {
  react: read(path.join(react, 'cjs/react.production.js')),
  'react/jsx-runtime': read(path.join(react, 'cjs/react-jsx-runtime.production.js')),
  'react-dom': read(path.join(reactDom, 'cjs/react-dom.production.js')),
  'react-dom/client': read(path.join(reactDom, 'cjs/react-dom-client.production.js')),
  scheduler: read(path.join(scheduler, 'cjs/scheduler.production.js')),
  './styles.css': ''
};
for (const [id, filename] of [['./api', 'api.ts'], ['./App', 'App.tsx'], ['./main', 'main.tsx']]) {
  modules[id] = ts.transpileModule(read(path.join(root, 'src', filename)), {
    fileName: filename,
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true
    }
  }).outputText;
}
const bundle = '/* Offline build from TypeScript and installed React production modules. */\n(()=>{\nconst modules = {' +
  Object.entries(modules).map(([id, code]) => JSON.stringify(id) + ':function(module,exports,require){\n' + code + '\n}').join(',\n') +
  '};\nconst cache={};function require(id){if(cache[id])return cache[id].exports;if(!modules[id])throw Error("Missing module: "+id);const module={exports:{}};cache[id]=module;modules[id](module,module.exports,require);return module.exports;}require("./main");\n})();\n';
const hash = crypto.createHash('sha256').update(bundle).digest('hex').slice(0, 12);
const assets = path.join(root, 'dist/assets');
fs.mkdirSync(assets, {recursive:true});
for (const filename of fs.readdirSync(assets)) {
  if (/^review-[a-f0-9]{12}\.(js|css)$/.test(filename)) fs.unlinkSync(path.join(assets, filename));
}
fs.writeFileSync(path.join(assets, `review-${hash}.js`), bundle);
fs.writeFileSync(path.join(assets, `review-${hash}.css`), fs.readFileSync(path.join(root, 'src/styles.css')));
const html = read(path.join(root, 'index.html'))
  .replace('<script type="module" src="/src/main.tsx"></script>', `<link rel="stylesheet" href="/assets/review-${hash}.css">\n    <script defer src="/assets/review-${hash}.js"></script>`);
fs.writeFileSync(path.join(root, 'dist/index.html'), html);
console.log(JSON.stringify({build:'offline_typescript_react', hash, bytes:Buffer.byteLength(bundle), output:path.join(root, 'dist/index.html')}));
