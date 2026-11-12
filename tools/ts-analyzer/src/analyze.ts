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

export function analyze(input: any) {
  if (!input || typeof input !== "object" || typeof input.root !== "string" || !path.isAbsolute(input.root))
    throw new InputError('"root" must be an absolute path string');
  if (!Array.isArray(input.files) || input.files.some((f: unknown) => typeof f !== "string"))
    throw new InputError('"files" must be an array of strings');
  if (!fs.existsSync(input.root) || !fs.statSync(input.root).isDirectory())
    throw new InputError(`root is not a directory: ${input.root}`);
  const root = fs.realpathSync(input.root);
  const diagnostics: string[] = [];
  if (input.files.length === 0) return { version: "1", files: [], diagnostics };

  // Validate file list.
  const rels: string[] = [];
  for (const f of input.files as string[]) {
    const abs = path.resolve(root, f);
    if (abs !== root && !abs.startsWith(root + path.sep)) { diagnostics.push(`skipped (outside root): ${f}`); continue; }
    if (!EXTS.test(f)) { diagnostics.push(`skipped (unsupported extension): ${f}`); continue; }
    if (!readable(root, abs)) { diagnostics.push(`skipped (not found, symlink or not a regular file): ${f}`); continue; }
    rels.push(path.relative(root, abs).split(path.sep).join("/"));
  }

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
  const program = ts.createProgram(rels.map((r) => path.join(root, r)), options, host);
  const checker = program.getTypeChecker();

  const relInRoot = (abs: string): string | null => {
    const p = path.resolve(abs);
    if (!p.startsWith(root + path.sep) || p.split(path.sep).includes("node_modules")) return null;
    return path.relative(root, p).split(path.sep).join("/");
  };

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
    const sf = program.getSourceFile(path.join(root, rel));
    if (!sf) { diagnostics.push(`not parsed: ${rel}`); continue; }
    try {
      files.push(analyzeFile(sf, rel));
    } catch (e) {
      // One file the checker cannot handle (seen: a minified vendor bundle overflowing the stack) must not sink the
      // whole program; like tsserver after a failed request, the same checker goes on (a fresh one would need a
      // second full type-check's memory: an 8k-file program already uses ~3.1 GB of heap).
      diagnostics.push(`not analyzed (${(e as Error).message}): ${rel}`);
    }
  }
  return { version: "1", files, diagnostics };

  function analyzeFile(sf: ts.SourceFile, rel: string) {
    const imports: any[] = [], symbols: any[] = [], calls: any[] = [], component_usages: any[] = [];
    const line = (n: ts.Node) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1;
    const endLine = (n: ts.Node) => sf.getLineAndCharacterOfPosition(n.getEnd()).line + 1;

    const modSym = checker.getSymbolAtLocation(sf);
    const exportedNames = new Set<string>(modSym ? checker.getExportsOfModule(modSym).map((s) => s.getName()) : []);

    const addImport = (spec: string, node: ts.Node) => {
      const r = ts.resolveModuleName(spec, sf.fileName, options, host).resolvedModule;
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
