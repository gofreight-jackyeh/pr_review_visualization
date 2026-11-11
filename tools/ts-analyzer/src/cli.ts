#!/usr/bin/env node
import { analyze, InputError } from "./analyze.js";

const chunks: Buffer[] = [];
process.stdin.on("data", (c) => chunks.push(c));
process.stdin.on("end", () => {
  try {
    const input = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    process.stdout.write(JSON.stringify(analyze(input)) + "\n");
  } catch (e) {
    process.stderr.write(`ts-analyzer: ${e instanceof SyntaxError ? "invalid JSON input: " : ""}${(e as Error).message}\n`);
    process.exit(e instanceof InputError || e instanceof SyntaxError ? 2 : 1);
  }
});
