# Pre-push review — 2026-11-27

## Management credential ownership correction

PASS. Removed webot/login-code.cmd and both webot viewing CLI actions. Combination startup calls Pero's access-code module and supplies its old encrypted file only as a migration candidate. webot retains separate OneBot transport-token responsibility. Updated the deployment guide to use Pero/login-code.cmd. Verified 24 offline cases, independent Pero lifecycle, combined lifecycle with a synthetic bridge, and successful protected API authentication using the code read through Pero's viewer. No actual WeChat or model interaction; old access-code values were not printed or recorded.

## Installer ownership correction

PASS. Per user request, install-pero.cmd is removed from webot and belongs to the Pero repository root; webot/install.cmd delegates to that script before installing bridge dependencies. Verified the adjacent default layout, a custom Pero path containing spaces, early stop when the Pero installer fails, and rejection of an incomplete standalone Pero checkout using temporary fixtures. No actual dependency installation or service startup was performed. Updated documentation and prerequisite diagnostics accordingly.

## Installation prerequisite follow-up

PASS for the installer/diagnostic change: added install-pero.cmd (backend requirements, editable package and npm frontend build) and install.cmd (Pero then bridge). Both stop on failures and never launch services. Launcher now distinguishes a missing repository, missing virtual environment and missing frontend build; stop/status remain usable without frontend assets. Verified invalid repository paths are rejected before installation, Python compilation and whitespace checks. Full dependency download/build remains the user's clean-deployment step; no pre-existing environment was copied into the fresh checkout.

## All-group startup revision (latest user decision)

PASS. Removed setup.cmd and terminal group-selection filtering. Startup automatically synchronizes all scanned groups, refreshes this account's group routes and removes groups missing from its startup snapshot, preserving other accounts and private routes. Historical group IDs still protect collision checks. Added install-bridge.cmd and actionable missing-dependency hints. Group membership changes require stop/start; no live membership hot reload is claimed. Verification: 24 offline cases, Python compilation/whitespace checks and real isolated Pero plus synthetic bridge startup/cooperative stop all passed. No real WeChat or model requests were used. Supersedes the earlier selection description below.

## Configuration helper follow-up

PASS: Added the user-requested double-click directory configuration helper and updated deployment instructions. Verified with Windows PowerShell 5.1 against temporary directories: first-run template creation, Chinese/space paths, preservation of unrelated fields, backup on update, invalid-root rejection, malformed-JSON preservation and no temporary files left behind. Errors do not echo raw configuration content. No real config or service was changed during verification. Script encoding includes UTF-8 BOM for PowerShell 5.1. Follow-up touches only the helper, its entry, ignore patterns and documentation.

Verdict: PASS for the local Windows deployment/ownership change.
Base branch: origin/main (937ca28d6513fd745bb2f9075c58b20e4c9a53ac).
Reviewed range: 937ca28d6513fd745bb2f9075c58b20e4c9a53ac..d779ac33ebb3e137104550d1d1098cc3175e9eb8. The following report commit contains documentation only.

## 1. Context and scope

User requested webot-owned combination orchestration, independent Pero, explicit WeChat binding, GitHub publication and a fresh checkout for manual installation. Reviewed against the Windows deployment guide and repository boundaries. Release sources were based on current remote main, not a blanket overwrite of a dirty working copy.

## 2. Plan adherence

webot owns preparation, explicit terminal group selection, supervisor, bridge and combined stop/status. Pero retains independent Core/Manager startup; its bridge pairing module, combination command and flags were removed. First selection is in setup.cmd, not a new frontend page. Restarts retain selected groups rather than enabling newly discovered groups.

## 3. Code quality and failure paths

Binding checks explicit account directory/internal identity, PID lifetime, desktop session and window/input ownership. Failed identity is latched. Database fallback is checked before exposing results. Supervisor uses owned Windows jobs and cooperative stop rather than killing process names. New default combination memory/cache is isolated from standalone Pero. Scope/application failures prevent bridge startup.

## 4. Commit hygiene

Reviewed changed text files for credential-shaped literals and oversized artifacts: no findings. No local config, credentials, chat history, binding, virtual environment, node_modules, frontend dist or runtime data was staged. The two removed legacy files were explicitly requested by the ownership migration.

## 5. Verification

- 25 focused offline tests passed: account binding, process/window scoping, sender-boundary rejection, explicit/reused group selection and send receipts.
- Python compilation and git diff whitespace checks passed.
- Real isolated Pero Core and Manager started on loopback test ports and stopped cooperatively.
- webot supervisor started real Pero with a synthetic bridge, observed its connection, then stopped the whole owned set cooperatively. No actual WeChat interaction, chat event or model request was used.
- Existing receipt fixtures were updated to supply the verified-target resolver expected by the current client; no-retry and delivery-result assertions remain intact.

## 6. Limitations and verdict

PASS for publication and clean installation simulation. Real WeChat login, process database-handle visibility, binding-code observation and dual-account/media sending still require the user's interactive desktop test. Do not describe those physical paths as already verified. No frontend redesign or new group-picker page was implemented. The fresh checkout is intentionally left without installed dependencies, configuration or running services.
