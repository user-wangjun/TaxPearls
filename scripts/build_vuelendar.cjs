/* Build the actual Vuelendar components and a private Vue runtime for the dashboard.
 * node scripts/build_vuelendar.cjs; no Node or CDN is required by the Python service.
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const {execSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const entry = path.join(root, 'webapp/static/vuelendar.mjs');
const output = path.join(root, 'webapp/static/vuelendar.bundle.js');
const versions = 'vuelendar@1.0.16 vue@3.5.45 @vue/compiler-sfc@3.5.45 esbuild@0.28.2';
function stamp() {
  const hash = crypto.createHash('sha256');
  for (const file of [__filename, entry]) hash.update(fs.readFileSync(file, 'utf8').replace(/\r\n/g, '\n'));
  return '// Vuelendar 1.0.16 / Vue 3.5.45; source-sha256=' + hash.digest('hex') + '\n';
}
function assertCurrent() {
  if (!fs.existsSync(output) || !fs.readFileSync(output, 'utf8').startsWith(stamp()))
    throw new Error('Run node scripts/build_vuelendar.cjs to refresh the Vuelendar bundle.');
}
async function build() {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'taxpearls-vuelendar-build-'));
  let esbuild;
  try {
    const network = process.argv.includes('--offline') ? '--offline ' : '--prefer-offline --fetch-retries=1 --fetch-timeout=30000 ';
    execSync('npm install --registry=https://registry.npmjs.org ' + network + '--ignore-scripts --no-audit --no-fund --package-lock=false --save-exact ' + versions,
      {cwd: temp, stdio: 'inherit', timeout: 180000});
    const modules = path.join(temp, 'node_modules');
    esbuild = require(path.join(modules, 'esbuild'));
    const compiler = require(path.join(modules, '@vue/compiler-sfc'));
    await esbuild.build({entryPoints: [entry], outfile: output, bundle: true, minify: true,
      format: 'iife', globalName: 'TaxPearlsLendar', target: 'es2020', nodePaths: [modules],
      define: {'process.env.NODE_ENV': '"production"', __VUE_OPTIONS_API__: 'true', __VUE_PROD_DEVTOOLS__: 'false', __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false'},
      banner: {js: stamp()}, legalComments: 'inline', plugins: [{
        name: 'vuelendar-sfc', setup(builder) {
          builder.onResolve({filter: /vl-(?:range-selector|calendar|calendar-month)$/}, args => ({
            path: require.resolve(args.path + '.vue', {paths: [args.resolveDir, modules]})}));
          builder.onLoad({filter: /\.vue$/}, args => {
            const {descriptor} = compiler.parse(fs.readFileSync(args.path, 'utf8'));
            let template = descriptor.template.content;
            template = template.replace('{{ monthName }} {{ year }}', '{{ year }}年 {{ monthName }}');
            // Upstream uses spans for clickable dates/arrows. Compile semantic
            // buttons locally so keyboard operation works without a runtime compiler.
            template = template.replace(/<span(\s+v-for="day in days"[\s\S]*?)<\/span>/,
              '<button type="button" :aria-label="getDate(day)" :aria-pressed="Boolean(isSelected && isSelected(getDate(day)))" :disabled="Boolean(isDisabled && isDisabled(getDate(day)))"$1</button>');
            template = template.replace(/<span(\s+class="vl-calendar__arrow vl-calendar__arrow--(back|forward)"[\s\S]*?)\/>/g,
              (_, attrs, direction) => '<button type="button" aria-label="' + (direction === 'back' ? '上个月' : '下个月') + '"' + attrs + '/>');
            const compiled = compiler.compileTemplate({source: template, filename: args.path, id: 'vuelendar'});
            if (compiled.errors.length) throw new Error(compiled.errors.join('\n'));
            const script = descriptor.script.content.replace('export default', 'const component =');
            return {contents: script + '\n' + compiled.code + '\ncomponent.render = render; export default component;',
              resolveDir: path.dirname(args.path), loader: 'js'};
          });
        }
      }]});
    const license = fs.readFileSync(path.join(modules, 'vuelendar/LICENSE'), 'utf8');
    fs.writeFileSync(path.join(root, 'webapp/static/vendor/VUELENDAR-LICENSE.txt'),
      'Vuelendar 1.0.16 — https://github.com/codesthq/vuelendar\n' + license +
      '\nVue 3.5.45 — https://github.com/vuejs/core\n' + fs.readFileSync(path.join(modules, 'vue/LICENSE'), 'utf8'));
    assertCurrent();
  } finally {
    if (esbuild) await esbuild.stop();
    if (path.dirname(temp) === path.resolve(os.tmpdir()) && path.basename(temp).startsWith('taxpearls-vuelendar-build-'))
      fs.rmSync(temp, {recursive: true, force: true, maxRetries: 5, retryDelay: 500});
  }
}
if (require.main === module) {
  if (process.argv.includes('--check')) assertCurrent();
  else build().catch(error => {console.error(error.message); process.exitCode = 1;});
}
module.exports = {assertCurrent};
