// Static analysis only: files are parsed via the TypeScript Compiler API, never executed/required/imported.
import ts from "typescript";
import fs from "node:fs";
import path from "node:path";

export class InputError extends Error {}

export interface Resolved { path: string; symbol: string }

const EXTS = /\.(ts|tsx|js|jsx|mjs|cjs)$/;
const MAX_BYTES = 1_000_000; // per file; keep in sync with MAX_BYTES in backend .../typescript/ts_bridge.py
const LIB_DIR = fs.realpathSync(path.dirname(ts.getDefaultLibFilePath({})));
const within = (p: string, dir: string) => p === dir || p.startsWith(dir + path.sep);

// The analyzed PR is untrusted: it can commit symlinks (`a.ts -> /etc/...`, `-> /dev/zero`) or FIFOs. Every compiler
// read goes through these two: a regular, non-symlink file whose real path is inside the repo root (or TypeScript's
// own lib directory), read at most MAX_BYTES. Anything else looks like a missing file to the compiler.
function readable(root: string, file: string): boolean {
  try {
    const abs = path.resolve(file);
    const st = fs.lstatSync(abs);
    if (st.isSymbolicLink() || !st.isFile()) return false;
    const real = fs.realpathSync(abs);
    return within(real, root) || within(real, LIB_DIR);
  } catch { return false; }
}

function safeRead(root: string, file: string): string | undefined {
  if (!readable(root, file)) return undefined;
  let fd: number | undefined;
  try {
    fd = fs.openSync(path.resolve(file), fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0));
    const st = fs.fstatSync(fd);
    if (!st.isFile()) return undefined;
    const buf = Buffer.alloc(Math.min(st.size, MAX_BYTES));
    let n = 0;
    for (let r = 1; n < buf.length && r > 0; n += r) r = fs.readSync(fd, buf, n, buf.length - n, null);
    const start = buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf ? 3 : 0; // strip UTF-8 BOM like ts.sys.readFile
    return buf.subarray(start, n).toString("utf8");
  } catch { return undefined; } finally { if (fd !== undefined) fs.closeSync(fd); }
}

// Input: {root, files, analyze?, meta?, context?, order?}. `files` are all TS/JS files; a full run makes all of them
// the program's roots. `analyze` limits the reported files. `meta: true` adds, per reported file, what its result
// depends on (see fileMeta) and moves per-file diagnostics to `file_diagnostics`, so the backend can cache each file's
// result and know when to recompute it.
// Incremental run (backend ts_bridge + ts_incremental): `context` lists the files adding to the global scope and
// `order` the module references (as fileMeta's `order`) of files not being analyzed. The program is then rooted at
// the `analyze` files plus `context` only (TypeScript loads the rest of their import closure itself), in the order
// the full program would load them, because global declarations merge in that order; see incrementalProgram.
// The CLI's output as one JSON string. Each file's result is serialized as soon as it is produced: an 8k-file
// program's results kept as live objects cost hundreds of MB of the heap the type checker needs.
export function analyzeToJson(input: any): string {
  const out: any = analyze(input, true);
  const files = out.files as string[];
  out.files = [];
  const s = JSON.stringify(out);
  const at = s.indexOf('"files":[]') + '"files":['.length;
  return s.slice(0, at) + files.join(",") + s.slice(at);
}

export function analyze(input: any, serialized = false) {
  if (!input || typeof input !== "object" || typeof input.root !== "string" || !path.isAbsolute(input.root))
    throw new InputError('"root" must be an absolute path string');
  if (!Array.isArray(input.files) || input.files.some((f: unknown) => typeof f !== "string"))
    throw new InputError('"files" must be an array of strings');
  if (input.analyze !== undefined && (!Array.isArray(input.analyze) || input.analyze.some((f: unknown) => typeof f !== "string")))
    throw new InputError('"analyze" must be an array of strings');
  if (input.context !== undefined && (!Array.isArray(input.context) || input.context.some((f: unknown) => typeof f !== "string")))
    throw new InputError('"context" must be an array of strings');
  if (input.context !== undefined && !Array.isArray(input.analyze))
    throw new InputError('"context" needs "analyze"');
  if (input.order !== undefined && (typeof input.order !== "object" || input.order === null || Array.isArray(input.order)))
    throw new InputError('"order" must be an object');
  const meta = input.meta === true;
  if (!fs.existsSync(input.root) || !fs.statSync(input.root).isDirectory())
    throw new InputError(`root is not a directory: ${input.root}`);
  const root = fs.realpathSync(input.root);
  const diagnostics: string[] = [];
  const fileDiagnostics: { path: string; kind: string; text: string; meta?: unknown }[] = [];
  const empty = () => meta ? { version: "1", files: [], diagnostics, file_diagnostics: fileDiagnostics } : { version: "1", files: [], diagnostics };
  if (input.files.length === 0) return empty();
  const wanted: Set<string> | null = input.analyze ? new Set(input.analyze as string[]) : null;
  const fileDiag = (rel: string, kind: string, text: string, sf?: ts.SourceFile) => {
    if (wanted && !wanted.has(rel)) return;
    if (!meta) { diagnostics.push(text); return; }
    let m: unknown;
    try { m = sf ? fileMeta(sf) : undefined; } catch { m = undefined; } // without it the backend never caches the file
    fileDiagnostics.push(m === undefined ? { path: rel, kind, text } : { path: rel, kind, text, meta: m });
  };

  // Validate file list.
  const rels: string[] = [];
  for (const f of input.files as string[]) {
    const abs = path.resolve(root, f);
    if (abs !== root && !abs.startsWith(root + path.sep)) { fileDiag(f, "skip", `skipped (outside root): ${f}`); continue; }
    if (!EXTS.test(f)) { fileDiag(f, "skip", `skipped (unsupported extension): ${f}`); continue; }
    if (!readable(root, abs)) { fileDiag(f, "skip", `skipped (not found, symlink or not a regular file): ${f}`); continue; }
    rels.push(path.relative(root, abs).split(path.sep).join("/"));
  }
  if (rels.length === 0) return empty();

  // Compiler options: honor root tsconfig (paths/baseUrl etc.), force safe settings.
  let options: ts.CompilerOptions = {};
  const readFile = (f: string) => safeRead(root, f);
  const fileExists = (f: string) => readable(root, f);
  const cfgPath = path.join(root, "tsconfig.json");
  if (fs.existsSync(cfgPath)) {
    const cfg = ts.readConfigFile(cfgPath, readFile);
    if (cfg.error) diagnostics.push(`tsconfig: ${ts.flattenDiagnosticMessageText(cfg.error.messageText, "\n")}`);
    else {
      const cfgHost: ts.ParseConfigHost = {
        useCaseSensitiveFileNames: ts.sys.useCaseSensitiveFileNames, readDirectory: ts.sys.readDirectory, readFile, fileExists,
      };
      const parsed = ts.parseJsonConfigFileContent(cfg.config, cfgHost, root, undefined, cfgPath);
      options = parsed.options;
    }
  }
  delete options.plugins; // never load analyzed project's plugins
  Object.assign(options, {
    noEmit: true, allowJs: true, skipLibCheck: true, checkJs: false,
    noResolve: false, types: options.types ?? [],
    jsx: options.jsx ?? ts.JsxEmit.Preserve,
    allowImportingTsExtensions: true,
  });
  delete options.composite; delete options.incremental; delete options.tsBuildInfoFile;
  delete options.declaration; delete options.declarationDir; delete options.outDir;

  const host = ts.createCompilerHost(options);
  host.readFile = readFile; // the default getSourceFile reads through host.readFile
  host.fileExists = fileExists;
  // Module resolution depends only on the importing file's directory (one set of options, no resolution mode):
  // memoized per directory + specifier, the same answer as resolving again (failed lookups included).
  const resolutions = new Map<string, ts.ResolvedModuleWithFailedLookupLocations>();
  const resolve = (spec: string, fileName: string) => {
    const k = `${path.dirname(fileName)}\0${spec}`;
    let r = resolutions.get(k);
    if (!r) resolutions.set(k, r = ts.resolveModuleName(spec, fileName, options, host));
    return r;
  };
  let program: ts.Program;
  let checker: ts.TypeChecker;
  if (input.context === undefined) {
    program = ts.createProgram(rels.map((r) => path.join(root, r)), options, host);
    checker = program.getTypeChecker();
  } else [program, checker] = incrementalProgram();

  function relInRoot(abs: string): string | null {
    const p = path.resolve(abs);
    if (!p.startsWith(root + path.sep) || p.split(path.sep).includes("node_modules")) return null;
    return path.relative(root, p).split(path.sep).join("/");
  }

  // The full program loads its roots (`rels`, sorted) depth-first, each file after its references and imports, and
  // the checker merges global declarations in that load order (a global declared twice resolves to the first). So an
  // incremental program must load the context files in the same relative order: roots ordered by a simulation of the
  // full load order (known references from `order`; the analyzed files' own references from this program), then
  // checked against the order TypeScript actually used. A mismatch rebuilds once with the corrected order; a second
  // mismatch throws (the backend then runs the full program).
  function incrementalProgram(): [ts.Program, ts.TypeChecker] {
    const known = input.order as Record<string, unknown> | undefined ?? {};
    const ctx = new Set<string>(input.context as string[]);
    const roots = (order: string[]) => order.filter((r) => ctx.has(r) || wanted!.has(r)).map((r) => path.join(root, r));
    const simulate = (depsOf: (rel: string) => string[]) => {
      const seen = new Set<string>(), out: string[] = [];
      for (const r of rels) {
        if (seen.has(r)) continue;
        seen.add(r);
        const stack: [string, string[], number][] = [[r, depsOf(r), 0]];
        while (stack.length) {
          const top = stack[stack.length - 1];
          if (top[2] < top[1].length) {
            const d = top[1][top[2]++];
            if (!seen.has(d)) { seen.add(d); stack.push([d, depsOf(d), 0]); }
          } else { stack.pop(); out.push(top[0]); }
        }
      }
      return out;
    };
    const cachedDeps = (rel: string) => { const v = known[rel]; return Array.isArray(v) ? v.filter((x) => typeof x === "string") : []; };
    let order = simulate(cachedDeps);
    for (let attempt = 0; ; attempt++) {
      const p = ts.createProgram(roots(order), options, host);
      const c = p.getTypeChecker(); // binds every file: module-ness and JS global augmentations are known from here
      for (const sf of p.getSourceFiles()) { const rel = relInRoot(sf.fileName); if (rel && contributesGlobals(sf)) ctx.add(rel); }
      const fromProgram = (rel: string) => { const sf = p.getSourceFile(path.join(root, rel)); return sf ? loadOrder(p, sf) : cachedDeps(rel); };
      order = simulate(fromProgram);
      const want = order.filter((r) => ctx.has(r));
      const got = p.getSourceFiles().map((sf) => relInRoot(sf.fileName)).filter((r): r is string => !!r && ctx.has(r));
      if (want.length === got.length && want.every((r, i) => r === got[i])) return [p, c];
      if (attempt > 0) throw new Error("incremental program cannot load global declarations in the full program's order");
    }
  }

  const resolveDecl = (sym: ts.Symbol | undefined): Resolved | null => {
    if (!sym) return null;
    if (sym.flags & ts.SymbolFlags.Alias) {
      try { sym = checker.getAliasedSymbol(sym); } catch { return null; }
    }
    const decl = sym.declarations?.[0];
    if (!decl) return null;
    const sf = decl.getSourceFile();
    if (sf.isDeclarationFile && !relInRoot(sf.fileName)) return null;
    const p = relInRoot(sf.fileName);
    if (!p) return null;
    let name = sym.getName();
    if (name === "default" || name.startsWith("__")) {
      const n = (decl as any).name;
      name = n && ts.isIdentifier(n) ? n.text : "default";
    }
    const par = decl.parent;
    if ((ts.isMethodDeclaration(decl) || ts.isPropertyDeclaration(decl) || ts.isConstructorDeclaration(decl)) &&
        (ts.isClassDeclaration(par) || ts.isClassExpression(par)) && par.name)
      name = `${par.name.text}.${ts.isConstructorDeclaration(decl) ? "constructor" : name}`;
    return { path: p, symbol: name };
  };

  const files: any[] = [];
  for (const rel of rels) {
    if (wanted && !wanted.has(rel)) continue;
    const sf = program.getSourceFile(path.join(root, rel));
    if (!sf) { fileDiag(rel, "fail", `not parsed: ${rel}`); continue; }
    try {
      const f: any = analyzeFile(sf, rel);
      if (meta) f.meta = fileMeta(sf);
      files.push(serialized ? JSON.stringify(f) : f);
    } catch (e) {
      // One file the checker cannot handle (seen: a minified vendor bundle overflowing the stack) must not sink the
      // whole program; like tsserver after a failed request, the same checker goes on (a fresh one would need a
      // second full type-check's memory: an 8k-file program already uses ~3.1 GB of heap).
      fileDiag(rel, "fail", `not analyzed (${(e as Error).message}): ${rel}`, sf);
    }
  }
  return meta ? { version: "1", files, diagnostics, file_diagnostics: fileDiagnostics } : { version: "1", files, diagnostics };

  // In-repo files TypeScript loads for this file, in its load order: `/// <reference path>` first, then module
  // references (imports, re-exports, import types, require/import(), JSDoc imports in JS) and module augmentations.
  function loadOrder(p: ts.Program, sf: ts.SourceFile): string[] {
    const out: string[] = [];
    const add = (f: ts.SourceFile | undefined) => { const rel = f && relInRoot(f.fileName); if (rel) out.push(rel); };
    for (const ref of sf.referencedFiles) {
      const abs = path.resolve(path.dirname(sf.fileName), ref.fileName);
      add(p.getSourceFile(abs) ?? p.getSourceFile(abs + ".ts") ?? p.getSourceFile(abs + ".d.ts"));
    }
    const js = /\.(js|jsx|mjs|cjs)$/.test(sf.fileName);
    const lits: ts.StringLiteralLike[] = [...((sf as any).imports ?? []), ...((sf as any).moduleAugmentations ?? []).filter(ts.isStringLiteralLike)];
    for (const l of lits) {
      if (!js && l.flags & ts.NodeFlags.JSDoc) continue; // TypeScript does not load JSDoc imports of TS files
      const r = resolve(l.text, sf.fileName).resolvedModule;
      if (r && !r.isExternalLibraryImport) add(p.getSourceFile(r.resolvedFileName));
    }
    return out;
  }

  // A file whose declarations reach the global scope: a script (no import/export/require), a JS file assigning to an
  // undeclared name's properties (`$.fn.x = ...`: binder jsGlobalAugmentations), `declare global` / `declare module`
  // blocks, or a UMD `export as namespace X`.
  function contributesGlobals(sf: ts.SourceFile): boolean {
    if (!(ts.isExternalModule(sf) || (sf as any).commonJsModuleIndicator)) return true;
    if ((sf as any).jsGlobalAugmentations?.size || (sf as any).symbol?.globalExports?.size) return true;
    return sf.statements.some((st) => ts.isModuleDeclaration(st) && (!!(st.flags & ts.NodeFlags.GlobalAugmentation) || ts.isStringLiteral(st.name)));
  }

  // What a file's result can depend on besides its own text, for the backend's per-file cache:
  // - module: false for a script (no import/export/require): its top-level names are global, like `declare global`
  //   blocks and module augmentations of a module; `context` marks every such file (an incremental program keeps them
  //   all, since any file may see them) and `globals` lists the names it adds.
  // - deps: in-repo files its module references resolved to (imports incl. `import()` types and JSDoc imports,
  //   re-exports, `/// <reference path>`, package.json files read while resolving).
  // - probes: in-repo paths module resolution looked for and did not find; one of them appearing changes resolution.
  // - vocab: identifiers it uses or declares (not property names) and its module specifiers (quoted), to match
  //   against other files' `globals`.
  function fileMeta(sf: ts.SourceFile) {
    const deps = new Set<string>(), probes = new Set<string>(), vocab = new Set<string>(), globals = new Set<string>();
    const isModule = !!(ts.isExternalModule(sf) || (sf as any).commonJsModuleIndicator);
    const context = contributesGlobals(sf);
    const addRel = (set: Set<string>, abs: string) => {
      if (abs.includes("/node_modules/")) return; // outside the analysis (relInRoot); bare specifiers probe dozens each
      const r = relInRoot(abs); if (r) set.add(r);
    };
    const specs: string[] = ((sf as any).imports ?? []).map((l: ts.StringLiteralLike) => l.text);
    for (const l of ((sf as any).moduleAugmentations ?? []) as ts.Node[]) if (ts.isStringLiteralLike(l)) specs.push(l.text);
    for (const spec of new Set(specs)) {
      vocab.add(JSON.stringify(spec));
      const r: any = resolve(spec, sf.fileName);
      if (r.resolvedModule && !r.resolvedModule.isExternalLibraryImport) addRel(deps, r.resolvedModule.resolvedFileName);
      for (const p of r.failedLookupLocations ?? []) addRel(probes, p);
      for (const p of r.affectingLocations ?? []) addRel(deps, p);
    }
    for (const ref of sf.referencedFiles) {
      const abs = path.resolve(path.dirname(sf.fileName), ref.fileName);
      if (program.getSourceFile(abs)) addRel(deps, abs); else addRel(probes, abs);
    }
    if (!isModule) for (const k of ((sf as any).locals as Map<string, unknown> | undefined)?.keys() ?? []) globals.add(k);
    // JS: `$.fn.x = ...` on an undeclared `$` declares a global `$` even in a module (binder: jsGlobalAugmentations)
    for (const k of ((sf as any).jsGlobalAugmentations as Map<string, unknown> | undefined)?.keys() ?? []) globals.add(k);
    for (const k of ((sf as any).symbol?.globalExports as Map<string, unknown> | undefined)?.keys() ?? []) globals.add(k);
    for (const st of sf.statements) {
      if (!ts.isModuleDeclaration(st) || !(st.flags & ts.NodeFlags.GlobalAugmentation || ts.isStringLiteral(st.name))) continue;
      if (ts.isStringLiteral(st.name)) globals.add(JSON.stringify(st.name.text));
      if (st.body && ts.isModuleBlock(st.body)) for (const d of st.body.statements) {
        if (ts.isVariableStatement(d)) d.declarationList.declarations.forEach((v) => ts.isIdentifier(v.name) && globals.add(v.name.text));
        else if ((d as any).name && ts.isIdentifier((d as any).name)) globals.add((d as any).name.text);
      }
    }
    const walk = (n: ts.Node): void => {
      if (ts.isIdentifier(n)) {
        const p = n.parent as any;
        const member = (ts.isPropertyAccessExpression(p) && p.name === n) || (ts.isQualifiedName(p) && p.right === n) ||
          ts.isJsxAttribute(p) || ((ts.isPropertyAssignment(p) || ts.isMethodDeclaration(p) || ts.isPropertyDeclaration(p) ||
          ts.isPropertySignature(p) || ts.isMethodSignature(p) || ts.isGetAccessor(p) || ts.isSetAccessor(p)) && p.name === n);
        if (!member) vocab.add(n.text);
      }
      ts.forEachChild(n, walk);
    };
    walk(sf);
    const rel = relInRoot(sf.fileName);
    if (rel) deps.delete(rel);
    return { module: isModule, context, globals: [...globals].sort(), deps: [...deps].sort(), probes: [...probes].sort(),
             vocab: [...vocab].sort(), order: loadOrder(program, sf) };
  }

  function analyzeFile(sf: ts.SourceFile, rel: string) {
    const imports: any[] = [], symbols: any[] = [], calls: any[] = [], component_usages: any[] = [];
    const line = (n: ts.Node) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1;
    const endLine = (n: ts.Node) => sf.getLineAndCharacterOfPosition(n.getEnd()).line + 1;

    const modSym = checker.getSymbolAtLocation(sf);
    const exportedNames = new Set<string>(modSym ? checker.getExportsOfModule(modSym).map((s) => s.getName()) : []);

    const addImport = (spec: string, node: ts.Node) => {
      const r = resolve(spec, sf.fileName).resolvedModule;
      let resolved: string | null = null;
      if (r && !r.isExternalLibraryImport) resolved = relInRoot(r.resolvedFileName);
      imports.push({ specifier: spec, resolved_path: resolved, line: line(node) });
    };

    const hasJsx = (n: ts.Node): boolean => {
      let found = false;
      const w = (x: ts.Node) => { if (found) return; if (ts.isJsxElement(x) || ts.isJsxSelfClosingElement(x) || ts.isJsxFragment(x)) { found = true; return; } ts.forEachChild(x, w); };
      w(n); return found;
    };
    const extendsComponent = (c: ts.ClassLikeDeclaration) =>
      !!c.heritageClauses?.some((h) => h.token === ts.SyntaxKind.ExtendsKeyword &&
        h.types.some((t) => /(^|\.)(Pure)?Component$/.test(t.expression.getText(sf))));
    const upper = (s: string) => /^[A-Z]/.test(s);

    const addSymbol = (name: string, kind: string, node: ts.Node, parent: string | null, exported: boolean) =>
      symbols.push({ name, kind, start_line: line(node), end_line: endLine(node), parent, exported });

    const visit = (node: ts.Node, cur: string | null, cls: string | null): void => {
      // imports
      if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) addImport(node.moduleSpecifier.text, node);
      else if (ts.isExportDeclaration(node) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) addImport(node.moduleSpecifier.text, node);
      else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference) && ts.isStringLiteral(node.moduleReference.expression)) addImport(node.moduleReference.expression.text, node);

      if (ts.isFunctionDeclaration(node) && node.body) {
        const name = node.name?.text ?? "default";
        addSymbol(name, upper(name) && hasJsx(node.body) ? "component" : "function", node, null, exportedNames.has(name) || (name === "default" && exportedNames.has("default")));
        return void ts.forEachChild(node, (c) => visit(c, name, null));
      }
      if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer &&
          (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))) {
        const name = node.name.text;
        const stmt = ts.isVariableDeclarationList(node.parent) && ts.isVariableStatement(node.parent.parent) ? node.parent.parent : node;
        const top = ts.isSourceFile(stmt.parent);
        if (top) addSymbol(name, upper(name) && hasJsx(node.initializer.body) ? "component" : "function", stmt, null, exportedNames.has(name));
        return void ts.forEachChild(node.initializer, (c) => visit(c, top ? name : cur, cls));
      }
      if ((ts.isClassDeclaration(node) || ts.isClassExpression(node)) && node.name) {
        const name = node.name.text;
        addSymbol(name, extendsComponent(node) ? "component" : "class", node, null, exportedNames.has(name));
        return void ts.forEachChild(node, (c) => visit(c, cur, name));
      }
      if (cls && (ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node)) && node.parent && (ts.isClassDeclaration(node.parent) || ts.isClassExpression(node.parent)) && node.body) {
        const m = ts.isConstructorDeclaration(node) ? "constructor" : node.name.getText(sf);
        addSymbol(m, "method", node, cls, exportedNames.has(cls));
        return void ts.forEachChild(node, (c) => visit(c, `${cls}.${m}`, cls));
      }
      if (cls && ts.isPropertyDeclaration(node) && node.initializer && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))) {
        const m = node.name.getText(sf);
        addSymbol(m, "method", node, cls, exportedNames.has(cls));
        return void ts.forEachChild(node.initializer, (c) => visit(c, `${cls}.${m}`, cls));
      }

      if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
        const e = node.expression;
        if (ts.isCallExpression(node) && e.kind === ts.SyntaxKind.ImportKeyword) {
          const a = node.arguments[0];
          if (a && ts.isStringLiteralLike(a)) addImport(a.text, node);
        } else if (ts.isCallExpression(node) && ts.isIdentifier(e) && e.text === "require" && node.arguments[0] && ts.isStringLiteralLike(node.arguments[0])) {
          addImport((node.arguments[0] as ts.StringLiteralLike).text, node);
        }
        // Element access with non-literal key (obj[name]()) has no static symbol -> null.
        const target = ts.isPropertyAccessExpression(e) ? e.name : ts.isIdentifier(e) ? e : undefined;
        const resolved = target ? resolveDecl(checker.getSymbolAtLocation(target)) : null;
        calls.push({ from_symbol: cur, callee_text: (ts.isNewExpression(node) ? "new " : "") + e.getText(sf), resolved, line: line(node) });
      }

      if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
        const tag = node.tagName;
        const text = tag.getText(sf);
        if (upper(text) && (ts.isIdentifier(tag) || ts.isPropertyAccessExpression(tag))) {
          const t = ts.isIdentifier(tag) ? tag : tag.name;
          component_usages.push({ from_symbol: cur, component: text, resolved: resolveDecl(checker.getSymbolAtLocation(t)), line: line(node) });
        }
      }
      ts.forEachChild(node, (c) => visit(c, cur, cls));
    };
    visit(sf, null, null);
    return { path: rel, imports, symbols, calls, component_usages };
  }
}
