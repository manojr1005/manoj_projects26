#!/usr/bin/env bash
# Creates (if needed) a Cognito test user and prints an ID token on stdout.
# Usage: eval "$(scripts/get_token.sh <stack-name> [email])"   -> exports ID_TOKEN
# Nothing is written to disk; the password is generated per call unless TEST_PASSWORD is set.
set -euo pipefail
STACK="${1:?stack name}"; EMAIL="${2:-reviewer@example.com}"
PASSWORD="${TEST_PASSWORD:-Rev!ew-$(date +%s)-Aa1}"
out() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
POOL=$(out UserPoolId); CLIENT=$(out UserPoolClientId)
if ! aws cognito-idp admin-get-user --user-pool-id "$POOL" --username "$EMAIL" >/dev/null 2>&1; then
  aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$EMAIL" --message-action SUPPRESS \
    --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true >/dev/null
fi
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$EMAIL" --password "$PASSWORD" --permanent
TOKEN=$(aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH --client-id "$CLIENT" \
  --auth-parameters USERNAME="$EMAIL",PASSWORD="$PASSWORD" --query AuthenticationResult.IdToken --output text)
echo "export ID_TOKEN='$TOKEN'"
echo "export API_URL='$(out ApiUrl)'"
