#!/usr/bin/env bash
# Deletes every ticket belonging to the demo users. Safe to run any time.
# Run from the repo root:  bash scripts/cleanup.sh
# Needs: AWS CLI profile with access to the stack, curl, python3.

STACK=${STACK:-support-tickets}
export AWS_PROFILE=${AWS_PROFILE:-support-tickets-deploy}
USERS=${USERS:-"demo-a@example.com demo-b@example.com"}

for email in $USERS; do
  echo "User: $email"

  out=$( eval "$(scripts/get_token.sh "$STACK" "$email")" && printf '%s %s' "$API_URL" "$ID_TOKEN" )
  if [ -z "$out" ]; then
    echo "  could not get a token (check AWS_PROFILE=$AWS_PROFILE and stack '$STACK')" >&2
    continue
  fi
  api=${out% *}
  token=${out#* }

  deleted=0
  for _ in $(seq 1 50); do
    ids=$(curl -s "$api/tickets" -H "Authorization: Bearer $token" \
      | python3 -c 'import sys,json; print(" ".join(i["ticketId"] for i in json.load(sys.stdin).get("items",[])))' 2>/dev/null)
    [ -z "$ids" ] && break
    for id in $ids; do
      curl -s -o /dev/null -X DELETE "$api/tickets/$id" -H "Authorization: Bearer $token"
      deleted=$((deleted+1))
    done
  done
  echo "  deleted $deleted ticket(s)"
done

echo "Done."