// electron-builder's afterSign hook: signs Cubes.app with one local, self-signed certificate.
//
// macOS remembers screen recording and accessibility against an app's signature. Without a
// developer certificate electron-builder signs ad hoc, which pins a grant to that one build's
// hash: every new build looked like a new app and the permission was gone again. Signing every
// build with the same certificate gives macOS a requirement that survives updates (identifier +
// certificate). The certificate is made on first use and kept in its own keychain under the
// app's data folder, never the login keychain. Nothing trusts it; it only has to stay the same.

const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const NAME = "Cubes Local Signing";
const DIR = path.join(os.homedir(), "Library", "Application Support", "com.ice.cubes.desktop", "signing");
const KEYCHAIN = path.join(DIR, "local.keychain-db");
// Local only: the password just keeps the keychain unlockable from this script.
const PASSWORD = "cubes-local";

function run(program, args, options = {}) {
  return execFileSync(program, args, { stdio: ["ignore", "pipe", "pipe"], ...options });
}

function ensureIdentity() {
  if (fs.existsSync(KEYCHAIN)) return;
  fs.mkdirSync(DIR, { recursive: true, mode: 0o700 });
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "cubes-sign-"));
  try {
    fs.writeFileSync(
      path.join(work, "cert.cnf"),
      [
        "[req]", "distinguished_name = dn", "x509_extensions = ext", "prompt = no",
        "[dn]", `CN = ${NAME}`,
        "[ext]", "basicConstraints = critical,CA:false", "keyUsage = critical,digitalSignature",
        "extendedKeyUsage = critical,codeSigning", "",
      ].join("\n"),
    );
    run("/usr/bin/openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
      "-config", path.join(work, "cert.cnf"), "-keyout", path.join(work, "key.pem"), "-out", path.join(work, "cert.pem")]);
    run("/usr/bin/openssl", ["pkcs12", "-export", "-inkey", path.join(work, "key.pem"), "-in", path.join(work, "cert.pem"),
      "-out", path.join(work, "id.p12"), "-passout", `pass:${PASSWORD}`]);
    run("security", ["create-keychain", "-p", PASSWORD, KEYCHAIN]);
    run("security", ["set-keychain-settings", KEYCHAIN]);
    run("security", ["unlock-keychain", "-p", PASSWORD, KEYCHAIN]);
    run("security", ["import", path.join(work, "id.p12"), "-k", KEYCHAIN, "-P", PASSWORD, "-T", "/usr/bin/codesign"]);
    run("security", ["set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", PASSWORD, KEYCHAIN]);
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
}

/** @param {{ appOutDir: string, packager: { appInfo: { productFilename: string, id: string } }, electronPlatformName: string }} context */
exports.default = async function sign(context) {
  if (context.electronPlatformName !== "darwin") return;
  const appPath = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  // CI builds get an ad-hoc signature so Apple Silicon will run them at all; Gatekeeper still
  // flags them as unverified (right-click → Open on first launch), which is expected until a
  // Developer ID certificate exists.
  if (process.env.CI) {
    run("/usr/bin/codesign", ["--force", "--deep", "--sign", "-", appPath]);
    console.log(`  • ad-hoc signed (CI)  ${appPath}`);
    return;
  }
  ensureIdentity();
  run("security", ["unlock-keychain", "-p", PASSWORD, KEYCHAIN]);
  // --deep signs the helpers, frameworks and the frozen backend inside too; the app itself keeps
  // the identifier from its Info.plist (com.ice.cubes.desktop).
  run("codesign", ["--force", "--deep", "--sign", NAME, "--keychain", KEYCHAIN, appPath]);
  console.log(`  • signed with the local certificate  ${appPath}`);
};
