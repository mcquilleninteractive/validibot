/**
 * Remove public-CDN references from generated developer documentation.
 *
 * Documentation diagrams are static SVG files and fonts are copied from exact
 * npm pins. This post-build guard neutralizes any dormant public-library URL
 * emitted by documentation tooling and then fails closed if a known public
 * CDN hostname remains in an executable generated asset.
 */

import {
    readFileSync,
    readdirSync,
    statSync,
    writeFileSync,
} from "node:fs";
import { dirname, extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const siteRoot = resolve(repositoryRoot, "docs_build/dev");
const blockedLibraryRoot = "/__self_host_required__/";
const executableExtensions = new Set([".html", ".js", ".mjs", ".css"]);
const forbiddenCdnHosts = [
    "unpkg.com",
    "cdn.jsdelivr.net",
    "cdnjs.cloudflare.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
];

function filesBelow(directory) {
    const paths = [];
    for (const name of readdirSync(directory)) {
        const path = resolve(directory, name);
        if (statSync(path).isDirectory()) {
            paths.push(...filesBelow(path));
        } else {
            paths.push(path);
        }
    }
    return paths;
}

const executableFiles = filesBelow(siteRoot).filter((path) =>
    executableExtensions.has(extname(path)),
);
let replacements = 0;
for (const path of executableFiles) {
    const original = readFileSync(path, "utf8");
    let hardened = original;
    const rewrites = [
        ["https://unpkg.com/", `${blockedLibraryRoot}unpkg/`],
        ["https://cdn.jsdelivr.net/", `${blockedLibraryRoot}jsdelivr/`],
        ["https://cdnjs.cloudflare.com/", `${blockedLibraryRoot}cdnjs/`],
    ];
    for (const [externalUrl, localUrl] of rewrites) {
        const occurrences = hardened.split(externalUrl).length - 1;
        hardened = hardened.replaceAll(externalUrl, localUrl);
        replacements += occurrences;
    }
    if (hardened !== original) {
        writeFileSync(path, hardened, "utf8");
    }
}

const offenders = [];
for (const path of executableFiles) {
    const contents = readFileSync(path, "utf8").toLowerCase();
    for (const host of forbiddenCdnHosts) {
        if (contents.includes(host)) {
            offenders.push(`${path}: ${host}`);
        }
    }
}
if (offenders.length > 0) {
    throw new Error(
        "Generated developer documentation references public browser CDNs:\n"
        + offenders.join("\n"),
    );
}

console.log(
    `Hardened developer documentation (${replacements} CDN fallback replacement(s)).`,
);
