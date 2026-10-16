# Garmin MCP Railwayssä

Tämä repo on kopio projektista [taxuspt/garmin_mcp](https://github.com/taxuspt/garmin_mcp)
(MIT-lisenssi), johon on lisätty Railway-hostausta varten:

- Docker-image käynnistyy oletuksena HTTP-tilassa (`streamable-http`, stateless)
  ja kuuntelee Railwayn antamaa `$PORT`-porttia.
- `MCP_AUTH_TOKEN` suojaa endpointin: pyynnöissä pitää olla
  `Authorization: Bearer <token>` tai `?key=<token>` URL:ssa.
- `GARMIN_TOKENS_BASE64` antaa Garmin-tokenit ympäristömuuttujana, koska
  Railwayssä ei voi syöttää MFA-koodia interaktiivisesti.
- `railway.json` määrittää Dockerfile-buildin ja `/healthz`-healthcheckin.

## 1. Luo Garmin-tokenit omalla koneellasi

```bash
uvx --python 3.12 --from git+https://github.com/OJHela/Garmin-mcp garmin-mcp-auth
cat ~/.garminconnect_base64
```

Kopioi tulostettu merkkijono talteen. Tokenit ovat voimassa noin 6 kuukautta.

## 2. Luo palvelu Railwayssä

1. Railway → **New Project → Deploy from GitHub repo** → valitse `OJHela/Garmin-mcp`.
2. **Variables**-välilehdelle:

   | Muuttuja | Arvo |
   |---|---|
   | `GARMIN_TOKENS_BASE64` | kohdan 1 merkkijono |
   | `MCP_AUTH_TOKEN` | pitkä satunnainen salasana, esim. `openssl rand -hex 32` |
   | `GARMIN_ENABLED_TOOLS` | *(valinnainen)* pilkuilla eroteltu lista työkaluista |

3. **Settings → Networking → Generate Domain**.
4. (Suositeltu) Lisää **Volume** polkuun `/root/.garminconnect`, jotta päivitetyt
   tokenit säilyvät uudelleenkäynnistysten yli.

Tarkista että palvelu on ylhäällä: `https://<domain>/readyz` palauttaa
`{"status": "ready"}`, kun Garmin-kirjautuminen onnistui.

## 3. Yhdistä MCP-asiakas

MCP-endpoint: `https://<domain>/mcp`

- **claude.ai (Custom connector)**: URL muodossa
  `https://<domain>/mcp?key=<MCP_AUTH_TOKEN>`
- **Claude Code**:
  ```bash
  claude mcp add --transport http garmin https://<domain>/mcp \
    --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
  ```

Pidä `MCP_AUTH_TOKEN` salassa — sillä pääsee lukemaan ja muokkaamaan Garmin-tiliäsi.
