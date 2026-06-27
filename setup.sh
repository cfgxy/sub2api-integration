#!/bin/bash
set -e
[ -f ".env" ] && set -a && source ".env" && set +a

EXTERNAL_URL="${EXTERNAL_URL:-http://192.168.2.226}"

echo -n "Waiting for settings table"
for i in $(seq 1 30); do
  TABLE=$(docker exec sub2api-postgres psql -U sub2api -d sub2api -tAc "SELECT 1 FROM pg_tables WHERE tablename='settings'" 2>/dev/null)
  if [ "$TABLE" = "1" ]; then
    echo
    break
  fi
  echo -n "."
  sleep 2
done

docker exec -i sub2api-postgres psql -U sub2api -d sub2api <<EOSQL
DELETE FROM settings WHERE key = 'custom_menu_items';
INSERT INTO settings (key, value) VALUES ('custom_menu_items',
  '[{"id":"nextchat","label":"Chat","icon_svg":"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z\"/></svg>","url":"${EXTERNAL_URL}/chat-bootstrap","visibility":"user","sort_order":10}]');
EOSQL

echo "Restarting Sub2API to pick up new settings..."
docker restart sub2api > /dev/null
sleep 3
echo "Done! Chat menu → ${EXTERNAL_URL}/chat-bootstrap"
