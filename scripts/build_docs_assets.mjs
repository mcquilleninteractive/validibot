/**
 * Vendor browser libraries required by the developer documentation.
 *
 * npm's lockfile pins the exact packages and integrity digests. This script
 * copies the reviewed font files, their licenses, and the application favicon
 * into the MkDocs/Zensical source tree so the generated site is self-contained.
 */

import {
    copyFileSync,
    mkdirSync,
    readFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const projectPackage = JSON.parse(
    readFileSync(resolve(repositoryRoot, "package.json"), "utf8"),
);

const fontDirectory = resolve(repositoryRoot, "docs/dev_docs/fonts");
mkdirSync(fontDirectory, { recursive: true });
const fontPackages = [
    {
        name: "@fontsource-variable/inter",
        files: [
            "inter-latin-ext-wght-normal.woff2",
            "inter-latin-ext-wght-italic.woff2",
            "inter-latin-wght-normal.woff2",
            "inter-latin-wght-italic.woff2",
        ],
        license: "INTER-OFL.txt",
    },
    {
        name: "@fontsource-variable/jetbrains-mono",
        files: [
            "jetbrains-mono-latin-ext-wght-normal.woff2",
            "jetbrains-mono-latin-ext-wght-italic.woff2",
            "jetbrains-mono-latin-wght-normal.woff2",
            "jetbrains-mono-latin-wght-italic.woff2",
        ],
        license: "JETBRAINS-MONO-OFL.txt",
    },
    {
        name: "@fontsource-variable/space-grotesk",
        files: [
            "space-grotesk-latin-ext-wght-normal.woff2",
            "space-grotesk-latin-wght-normal.woff2",
        ],
        license: "SPACE-GROTESK-OFL.txt",
    },
];

for (const fontPackage of fontPackages) {
    const packageVersion = projectPackage.devDependencies[fontPackage.name];
    if (!/^\d+\.\d+\.\d+$/.test(packageVersion)) {
        throw new Error(
            `${fontPackage.name} must use an exact version, received: `
            + packageVersion,
        );
    }
    const packageRoot = resolve(
        repositoryRoot,
        "node_modules",
        fontPackage.name,
    );
    const installedPackage = JSON.parse(
        readFileSync(resolve(packageRoot, "package.json"), "utf8"),
    );
    if (installedPackage.version !== packageVersion) {
        throw new Error(
            `${fontPackage.name} does not match package.json. `
            + "Run npm ci before building documentation assets.",
        );
    }
    for (const file of fontPackage.files) {
        copyFileSync(
            resolve(packageRoot, "files", file),
            resolve(fontDirectory, file),
        );
    }
    copyFileSync(
        resolve(packageRoot, "LICENSE"),
        resolve(fontDirectory, fontPackage.license),
    );
}

copyFileSync(
    resolve(repositoryRoot, "validibot/static/images/favicons/favicon.png"),
    resolve(repositoryRoot, "docs/dev_docs/images/favicon.png"),
);

console.log(
    "Vendored fonts and favicon for developer documentation.",
);
