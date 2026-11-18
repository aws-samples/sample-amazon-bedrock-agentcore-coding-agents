#!/bin/bash
# The check toolchain (November 18): what a validator-authored acceptance check may use
# without installing anything, a headless Chromium driven by Playwright and jsdom, at
# the SAME pinned versions in the coordinator (where the engine runs every check) and
# in each checker's own container (where the checker self-tests it). On November 17,
# checks that installed a browser or a DOM library at check time failed on their own
# setup in 5 of 60 team builds (a browser cached under the wrong path, a second jsdom
# import that could not resolve, a missing-capability failure), and each stopped a
# build. Installed under /opt and world-readable, for root and for the agent user.
#
# The image must also set:
#   ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright NODE_PATH=/opt/check-tools/node_modules
# and reviewer._CHECK_ENV_NAMES passes both to the check process.
set -euo pipefail
PLAYWRIGHT_VERSION=1.65.1
JSDOM_VERSION=31.0.1

dnf install -y --allowerasing \
    nss nspr atk at-spi2-atk cups-libs libdrm libxkbcommon libXcomposite libXdamage \
    libXext libXfixes libXrandr mesa-libgbm pango cairo alsa-lib libxshmfence libX11 \
    libxcb dbus-libs expat glib2
dnf clean all
mkdir -p /opt/check-tools
cd /opt/check-tools
echo '{"name": "check-tools", "private": true}' > package.json
npm install --no-audit --no-fund --omit=dev \
    "playwright@${PLAYWRIGHT_VERSION}" "jsdom@${JSDOM_VERSION}"
PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    node node_modules/playwright/cli.js install --only-shell chromium
chmod -R a+rX /opt/check-tools /opt/ms-playwright

# Prove it from an unrelated directory, the way a check resolves it: a real click in
# headless Chromium, and jsdom, through NODE_PATH alone.
cd /tmp
NODE_PATH=/opt/check-tools/node_modules PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright node -e '
const { chromium } = require("playwright");
const { JSDOM } = require("jsdom");
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.setContent("<button id=b onclick=\"this.textContent=String(1+1)\">start</button>");
  await page.click("#b");
  const text = await page.textContent("#b");
  await browser.close();
  const dom = new JSDOM("<p id=x>ok</p>");
  if (text !== "2" || dom.window.document.getElementById("x").textContent !== "ok") {
    console.error("check toolchain smoke test failed:", text);
    process.exit(1);
  }
  console.log("check toolchain ready: Playwright Chromium click and jsdom");
})().catch(error => { console.error("check toolchain smoke test failed:", error.message); process.exit(1); });
'
