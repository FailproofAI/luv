/**
 * version-policies.mjs — remind the agent to bump the release version.
 *
 * Auto-loaded by failproofai from .failproofai/policies/ — no install step.
 *
 * Publishing here is driven by the version in the manifest: a push to main
 * whose version has no tag yet releases itself (.github/workflows/publish.yml).
 * So a branch that changes the tool and forgets the version merges into a main
 * that publishes nothing, and the change sits unreleased until someone notices.
 *
 * So this says so on `git commit`, and only while the branch's version still
 * matches the base branch's — it is context, not a block, and bumping the
 * version is what turns it off for the rest of the branch.
 */
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { customPolicies, allow, instruct } from "failproofai";

// First match wins, so the order is "most likely to be the release manifest".
const MANIFESTS = [
  { file: "pyproject.toml", re: /^\s*version\s*=\s*["']([^"']+)["']/m },
  { file: "package.json", re: /"version"\s*:\s*"([^"]+)"/ },
  { file: "Cargo.toml", re: /^\s*version\s*=\s*["']([^"']+)["']/m },
];

const BASE_REFS = ["origin/main", "main", "origin/master", "master"];

// A `git … commit` that starts a command, so a shell segment is allowed to
// carry flags and their values (`git -C dir commit`) but a `commit` merely
// spoken about (`echo "run git commit later"`) is not mistaken for one.
const GIT_COMMIT = /(?:^|[;&|\n(]|&&|\|\|)\s*(?:\w+=\S+\s+)*git\b[^;&|\n]*?\bcommit\b/;

/** git, or null on any failure — this policy never gets in the way. */
function git(cwd, args) {
  try {
    return execFileSync("git", args, {
      cwd,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 5000,
    }).trim();
  } catch {
    return null;
  }
}

function versionIn(text, re) {
  const m = text == null ? null : text.match(re);
  return m ? m[1] : null;
}

/** The manifest this repo releases from, with the version on both sides. */
function releaseVersions(cwd) {
  const base = BASE_REFS.find((ref) =>
    git(cwd, ["rev-parse", "--verify", "--quiet", ref]) !== null);
  if (!base) return null;

  for (const { file, re } of MANIFESTS) {
    if (!existsSync(join(cwd, file))) continue;
    let head;
    try {
      head = versionIn(readFileSync(join(cwd, file), "utf8"), re);
    } catch {
      return null;
    }
    // No version on the base side means the manifest is new on this branch;
    // there is nothing to bump relative to.
    const baseVersion = versionIn(git(cwd, ["show", `${base}:${file}`]), re);
    if (!head || !baseVersion) return null;
    return { file, base, head, baseVersion };
  }
  return null;
}

customPolicies.add({
  name: "remind-version-bump-on-commit",
  description: "On git commit, remind that the release version is still the base branch's",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (ctx.toolName !== "Bash") return allow();
    const cmd = String(ctx.toolInput?.command ?? "");
    if (!GIT_COMMIT.test(cmd)) return allow();

    const cwd = ctx.session?.cwd ?? process.cwd();

    // Committing on the base branch is a different problem, and one the
    // block-work-on-main policy already has an opinion about.
    const branch = git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"]);
    if (branch === null || branch === "main" || branch === "master") return allow();

    const found = releaseVersions(cwd);
    if (!found) return allow();
    if (found.head !== found.baseVersion) return allow(); // already bumped

    return instruct(
      `${found.file} still says version ${found.head}, the same as ${found.base}. ` +
      "Releases are cut from that version when this branch lands, so a merge " +
      "with it unchanged publishes nothing. Bump it in this branch — patch for " +
      "a fix or polish, minor for a feature — unless the change is docs, tests, " +
      "or CI only, in which case say so and carry on."
    );
  },
});

export { customPolicies };
