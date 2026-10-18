import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Plugin, ResolvedConfig } from 'vite';

const frontendRoot = fileURLToPath(new URL('../', import.meta.url));
const compare = (a: string, b: string) => a < b ? -1 : a > b ? 1 : 0;
const sha256 = (text: string) => createHash('sha256').update(text).digest('hex');
const noticeName = /^(?:licen[sc]e|copying|notice|copyright)(?:[._-]|$)/i;

interface ReviewedException {
  package_json_sha256: string;
  notice: string;
  notice_sha256: string;
  upstream_metadata: string;
  upstream_metadata_sha256: string;
  source: string;
  reason: string;
}

interface IncludedPackage {
  name: string;
  version: string;
  license: string;
  modules: Set<string>;
  cssInputs: Set<string>;
  chunks: Set<string>;
  notices: { file: string; sha256: string; text: string }[];
  reviewedException?: ReviewedException;
}

/** Preserve notices for packages contributing production JS or imported CSS. */
export function productionLicenses(): Plugin {
  let resolveCss: ReturnType<ResolvedConfig['createResolver']>;
  return {
    name: 'production-dependency-licenses',
    configResolved(config) {
      resolveCss = config.createResolver({
        extensions: ['.css'], mainFields: ['style'], conditions: ['style'],
      });
    },
    async generateBundle(_options, bundle) {
      const exceptions = JSON.parse(readFileSync(
        resolve(frontendRoot, 'vendor/license-exceptions.json'), 'utf8',
      )) as Record<string, ReviewedException>;
      const packagesByRoot = new Map<string, IncludedPackage>();

      const include = (id: string, kind: 'module' | 'css', chunk?: string) => {
        const clean = id.replaceAll('\\', '/').split('?')[0];
        const match = [...clean.matchAll(/(?:^|\/)node_modules\/((?:@[^/]+\/)?[^/]+)/g)].at(-1);
        if (!match) return;
        if (clean.includes('\0')) throw new Error('Cannot identify the owner of a virtual npm module.');
        const root = clean.slice(0, match.index! + match[0].length);
        let entry = packagesByRoot.get(root);
        if (!entry) {
          const packageText = readFileSync(resolve(root, 'package.json'), 'utf8');
          const pkg = JSON.parse(packageText) as { name: string; version: string; license: string };
          if (!pkg.name || !pkg.version || typeof pkg.license !== 'string') {
            throw new Error(`Missing package/license metadata for ${match[1]}.`);
          }
          const notices = readdirSync(root, { withFileTypes: true })
            .filter(file => file.isFile() && noticeName.test(file.name))
            .map(file => {
              const text = readFileSync(resolve(root, file.name), 'utf8');
              if (!text.trim()) throw new Error(`Empty notice: ${pkg.name}/${file.name}.`);
              return { file: file.name, sha256: sha256(text), text };
            }).sort((a, b) => compare(a.file, b.file));
          let reviewedException: ReviewedException | undefined;
          if (!notices.some(notice => /^(?:licen[sc]e|copying)(?:[._-]|$)/i.test(notice.file))) {
            reviewedException = exceptions[`${pkg.name}@${pkg.version}`];
            if (!reviewedException || reviewedException.package_json_sha256 !== sha256(packageText)) {
              throw new Error(`No license text or reviewed exact-version exception for ${pkg.name}@${pkg.version}.`);
            }
            const noticePath = resolve(frontendRoot, reviewedException.notice);
            const relativePath = relative(resolve(frontendRoot, 'vendor'), noticePath);
            if (relativePath.startsWith('..') || !existsSync(noticePath)) {
              throw new Error(`Invalid reviewed notice for ${pkg.name}@${pkg.version}.`);
            }
            const text = readFileSync(noticePath, 'utf8');
            if (sha256(text) !== reviewedException.notice_sha256) {
              throw new Error(`Reviewed notice hash changed for ${pkg.name}@${pkg.version}.`);
            }
            const metadataPath = resolve(frontendRoot, reviewedException.upstream_metadata);
            if (relative(resolve(frontendRoot, 'vendor'), metadataPath).startsWith('..') ||
                sha256(readFileSync(metadataPath, 'utf8')) !== reviewedException.upstream_metadata_sha256) {
              throw new Error(`Reviewed upstream metadata changed for ${pkg.name}@${pkg.version}.`);
            }
            notices.push({ file: reviewedException.notice, sha256: sha256(text), text });
          }
          entry = { name: pkg.name, version: pkg.version, license: pkg.license,
            modules: new Set(), cssInputs: new Set(), chunks: new Set(), notices, reviewedException };
          packagesByRoot.set(root, entry);
        }
        const packagePath = clean.slice(root.length + 1);
        (kind === 'css' ? entry.cssInputs : entry.modules).add(packagePath);
        if (chunk) entry.chunks.add(chunk);
      };

      for (const chunk of Object.values(bundle)) {
        if (chunk.type !== 'chunk') continue;
        for (const [id, rendered] of Object.entries(chunk.modules)) {
          if (rendered.renderedLength > 0) include(id, 'module', chunk.fileName);
        }
      }

      // Vite/Tailwind inline CSS imports, so their package owners are absent
      // from JS chunk.modules. Follow the original imported CSS sources too.
      const seenCss = new Set<string>();
      const includeCss = async (id: string): Promise<void> => {
        const clean = id.split('?')[0];
        if (seenCss.has(clean)) return;
        seenCss.add(clean);
        include(clean, 'css');
        const css = readFileSync(clean, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
        const imports = css.matchAll(/@import\s+(?:url\(\s*)?(?:"([^"]+)"|'([^']+)'|([^\s;)]+))/g);
        for (const match of imports) {
          const specifier = match[1] ?? match[2] ?? match[3];
          if (/^(?:https?:|data:|\/\/)/i.test(specifier)) {
            throw new Error('Remote CSS imports require a separately reviewed license source.');
          }
          const dependency = await resolveCss(specifier, clean);
          if (!dependency) throw new Error(`Cannot resolve CSS import ${specifier} for license collection.`);
          await includeCss(dependency);
        }
      };
      if (Object.values(bundle).some(file => file.type === 'asset' && file.fileName.endsWith('.css'))) {
        for (const id of this.getModuleIds()) {
          if (/\.css(?:\?|$)/i.test(id)) await includeCss(id);
        }
      }

      const packages = [...packagesByRoot.values()].sort((a, b) =>
        compare(a.name, b.name) || compare(a.version, b.version));
      if (!packages.length) throw new Error('No production dependency licenses were collected.');
      const manifest = {
        schema_version: 1,
        scope: 'Rendered production JavaScript modules and imported CSS source packages; excludes build/test-only packages.',
        packages: packages.map(pkg => ({
          name: pkg.name, version: pkg.version, license: pkg.license,
          modules: [...pkg.modules].sort(compare), css_inputs: [...pkg.cssInputs].sort(compare),
          chunks: [...pkg.chunks].sort(compare),
          notices: pkg.notices.map(({ file, sha256 }) => ({ file, sha256 })),
          ...(pkg.reviewedException ? { reviewed_exception: pkg.reviewedException } : {}),
        })),
      };
      const text = ['Production dependency licenses',
        'Generated from exact installed packages contributing rendered JS or imported CSS.',
        'Source-copied assistant-ui components retain their separate assistant-ui-LICENSE.txt.',
        'The JSON manifest records versions, package-relative source paths and notice hashes.', '',
        ...packages.flatMap(pkg => [
          '='.repeat(72), `${pkg.name}@${pkg.version} (${pkg.license})`,
          ...(pkg.reviewedException ? [`Reviewed exception: ${pkg.reviewedException.reason}`,
            `Source: ${pkg.reviewedException.source}`] : []),
          ...pkg.notices.flatMap(notice => [`--- ${notice.file} (SHA-256 ${notice.sha256}) ---`, notice.text]), '',
        ]),
        'End of production dependency licenses.',
      ].join('\n') + '\n';
      this.emitFile({ type: 'asset', fileName: 'assets/third-party-licenses.txt', source: text });
      this.emitFile({ type: 'asset', fileName: 'assets/third-party-license-manifest.json',
        source: JSON.stringify(manifest, null, 2) + '\n' });
      this.info(`Preserved production license notices for ${packages.length} packages.`);
    },
  };
}
