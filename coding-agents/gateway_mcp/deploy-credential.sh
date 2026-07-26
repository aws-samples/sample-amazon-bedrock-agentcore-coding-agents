#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/config.sh"

SECRET_NAME="agentcore/github-mcp/github-app"

echo "==> Deploying GitHub App Secret to Secrets Manager: ${SECRET_NAME}"

# Validate required env vars
if [[ -z "${GITHUB_APP_ID:-}" ]]; then
  echo "ERROR: Set GITHUB_APP_ID (the numeric ID of your GitHub App)."
  exit 1
fi

if [[ -z "${GITHUB_APP_PRIVATE_KEY_FILE:-}" ]]; then
  echo "ERROR: Set GITHUB_APP_PRIVATE_KEY_FILE (path to .pem file)."
  exit 1
fi

if [[ ! -f "$GITHUB_APP_PRIVATE_KEY_FILE" ]]; then
  echo "ERROR: Private key file not found: ${GITHUB_APP_PRIVATE_KEY_FILE}"
  exit 1
fi

if [[ -z "${GITHUB_APP_INSTALLATION_ID:-}" ]]; then
  echo "ERROR: Set GITHUB_APP_INSTALLATION_ID (the installation ID for your org/repo)."
  exit 1
fi

if [[ ! "$GITHUB_APP_ID" =~ ^[0-9]+$ ]]; then
  echo "ERROR: GITHUB_APP_ID must contain digits only."
  exit 1
fi

if [[ ! "$GITHUB_APP_INSTALLATION_ID" =~ ^[0-9]+$ ]]; then
  echo "ERROR: GITHUB_APP_INSTALLATION_ID must contain digits only."
  exit 1
fi

if ! head -n 1 "$GITHUB_APP_PRIVATE_KEY_FILE" | grep -Eq '^-----BEGIN (RSA )?PRIVATE KEY-----$' \
  || ! tail -n 1 "$GITHUB_APP_PRIVATE_KEY_FILE" | grep -Eq '^-----END (RSA )?PRIVATE KEY-----$'; then
  echo "ERROR: Private key is incomplete: expected matching BEGIN/END PRIVATE KEY lines." >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "ERROR: openssl is required to validate the GitHub App private key." >&2
  exit 1
fi
if ! openssl pkey -in "$GITHUB_APP_PRIVATE_KEY_FILE" -check -noout >/dev/null 2>&1; then
  echo "ERROR: Private key cannot be parsed. Re-download it and repeat the paste step." >&2
  exit 1
fi
chmod 600 "$GITHUB_APP_PRIVATE_KEY_FILE"
echo "Private key validated: $(wc -l < "$GITHUB_APP_PRIVATE_KEY_FILE" | tr -d ' ') lines"

# Read the private key
PRIVATE_KEY=$(cat "$GITHUB_APP_PRIVATE_KEY_FILE")

# ---------------------------------------------------------------------------------
# PROVE the three values belong together, BEFORE writing them to Secrets Manager.
#
# The digits-only checks above cannot catch the mistake attendees actually make: the App
# ID and the installation ID are BOTH plain numbers read from two different GitHub pages,
# so transposing them passes every local check. The credential is then stored wrong and
# the failure surfaces much later as an opaque JWT/404 error from inside the MCP server,
# after the gateway has been deployed and a build has run its agents.
#
# So do what the MCP server will do: sign a JWT with the key and ask GitHub who we are.
# GET /app answers only for a correct (app_id, private_key) pair, and
# GET /app/installations/<id> answers only if that installation belongs to THIS app.
# Skipped (with a warning, never a hard fail) when python3 has no JWT support or the box
# has no egress, because this is a safety net and must not become a new way to be blocked.
_verify_app_credential() {
  python3 - "$GITHUB_APP_ID" "$GITHUB_APP_INSTALLATION_ID" "$GITHUB_APP_PRIVATE_KEY_FILE" <<'PYEOF'
import base64, json, sys, time, urllib.error, urllib.request
app_id, inst_id, key_path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError:
    print("SKIP: python cryptography not available; cannot pre-verify the App credential")
    raise SystemExit(2)

def b64(raw): return base64.urlsafe_b64encode(raw).rstrip(b"=")
now = int(time.time())
head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
body = b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
with open(key_path, "rb") as fh:
    key = serialization.load_pem_private_key(fh.read(), password=None)
sig = b64(key.sign(head + b"." + body, padding.PKCS1v15(), hashes.SHA256()))
jwt = (head + b"." + body + b"." + sig).decode()

def get(url):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json",
        "User-Agent": "agentcore-workshop"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

try:
    app = get("https://api.github.com/app")
except urllib.error.HTTPError as e:
    if e.code == 401:
        print(f"FAIL: GitHub rejected App ID {app_id} signed with this key (401).")
        print("      Either GITHUB_APP_ID is wrong (did you paste the INSTALLATION id?)")
        print("      or the .pem belongs to a different App.")
        raise SystemExit(1)
    print(f"SKIP: could not reach GitHub to verify (HTTP {e.code})"); raise SystemExit(2)
except Exception as exc:
    print(f"SKIP: could not reach GitHub to verify ({exc})"); raise SystemExit(2)

try:
    get(f"https://api.github.com/app/installations/{inst_id}")
except urllib.error.HTTPError as e:
    if e.code in (403, 404):
        print(f"FAIL: installation {inst_id} does not belong to App "
              f"'{app.get('slug')}' (id {app.get('id')}).")
        print("      GITHUB_APP_INSTALLATION_ID is the number at the end of")
        print("      https://github.com/settings/installations/<ID>, not the App ID.")
        raise SystemExit(1)
    print(f"SKIP: could not verify the installation (HTTP {e.code})"); raise SystemExit(2)
except Exception as exc:
    print(f"SKIP: could not verify the installation ({exc})"); raise SystemExit(2)

print(f"Verified with GitHub: App '{app.get('slug')}' (id {app.get('id')}) "
      f"and installation {inst_id} match this private key.")
PYEOF
}
set +e
_verify_app_credential
_verify_rc=$?
set -e
if [[ "$_verify_rc" == "1" ]]; then
  echo "ERROR: the App ID, installation ID and private key do not belong together." >&2
  echo "       Fixing this now saves you the 10 minutes a build takes before the PR step." >&2
  exit 1
fi

# Build the secret JSON value
SECRET_VALUE=$(jq -n \
  --arg app_id "$GITHUB_APP_ID" \
  --arg private_key "$PRIVATE_KEY" \
  --arg installation_id "$GITHUB_APP_INSTALLATION_ID" \
  '{app_id: $app_id, private_key: $private_key, installation_id: $installation_id}')

# Check if secret already exists
EXISTING=$(aws secretsmanager describe-secret \
  --secret-id "$SECRET_NAME" \
  --region "$AWS_REGION" 2>/dev/null || true)

if [[ -n "$EXISTING" ]]; then
  echo "Secret '${SECRET_NAME}' already exists. Updating..."
  aws secretsmanager put-secret-value \
    --secret-id "$SECRET_NAME" \
    --secret-string "$SECRET_VALUE" \
    --region "$AWS_REGION"
  SECRET_ARN=$(echo "$EXISTING" | jq -r '.ARN')
else
  echo "Creating secret '${SECRET_NAME}'..."
  CREATE_RESPONSE=$(aws secretsmanager create-secret \
    --name "$SECRET_NAME" \
    --description "GitHub App credentials for AgentCore GitHub MCP server" \
    --secret-string "$SECRET_VALUE" \
    --region "$AWS_REGION" \
    --output json)
  SECRET_ARN=$(echo "$CREATE_RESPONSE" | jq -r '.ARN')
fi

state_set "github_app_secret_arn" "$SECRET_ARN"
echo "Secret ARN: ${SECRET_ARN}"

echo ""
echo "==> GitHub App secret deployment complete."
echo "    Set GITHUB_APP_SECRET_ARN=${SECRET_ARN} or it will be read from state."
