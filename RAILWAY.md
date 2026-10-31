# Garmin MCP Railwayssä

Tämä repo on kopio projektista [taxuspt/garmin_mcp](https://github.com/taxuspt/garmin_mcp)
(MIT-lisenssi), johon on lisätty Railway-hostausta varten:

- Docker-image käynnistyy oletuksena HTTP-tilassa (`streamable-http`, stateless)
  ja kuuntelee Railwayn antamaa `$PORT`-porttia.
- `MCP_AUTH_TOKEN` suojaa endpointin: pyynnöissä pitää olla
  `Authorization: Bearer <token>` tai `?key=<token>` URL:ssa.
- **Kirjautuminen MCP:n kautta**: sano Claudelle esim. "kirjaudu Garminiin".
  Claude kysyy sähköpostin, salasanan ja tarvittaessa vahvistuskoodin ja
  kutsuu työkaluja `garmin_login` ja `garmin_submit_mfa_code`. Railwayssä ei
  tarvitse tehdä mitään. `garmin_login_status` kertoo, onko palvelin kirjautunut.
- Kirjautumissivu `/login` selaimessa (vain kun `MCP_AUTH_TOKEN` on asetettu).
- `railway.json` määrittää Dockerfile-buildin ja `/healthz`-healthcheckin.

## Pikaohje: kirjautuminen ilman Railway-asetuksia

1. Yhdistä MCP sovellukseesi osoitteella `https://<domain>/mcp`.
2. Pyydä Claudea kirjautumaan Garminiin ja anna sille sähköposti, salasana ja
   Garminin lähettämä vahvistuskoodi.

Salasanaa ei tallenneta, vain kirjautumistokenit (voimassa noin 6 kk). Huomaa:

- Ilman Volumea tokenit katoavat, kun Railway käynnistää palvelun uudelleen
  (esim. uusi deploy). Silloin Claude pyytää kirjautumaan uudelleen.
- Salasana kulkee keskustelun kautta, joten se jää keskusteluhistoriaan.
- Ilman `MCP_AUTH_TOKEN`-muuttujaa kuka tahansa, joka tietää palvelun
  osoitteen, voi käyttää kirjautunutta Garmin-tiliäsi. Suojaus vaatii alla
  olevat Railway-asetukset.

## 1. Railwayn asetukset (suositeltu suojaus)

1. **Variables**-välilehdelle:

   | Muuttuja | Arvo |
   |---|---|
   | `MCP_AUTH_TOKEN` | pitkä satunnainen salasana, esim. `openssl rand -hex 32` |
   | `GARMIN_ENABLED_TOOLS` | *(valinnainen)* pilkuilla eroteltu lista työkaluista |

2. **Settings → Networking → Generate Domain**.
3. **Volume** polkuun `/root/.garminconnect` (oikealla klikkaus palvelun
   kohdalla → *Attach Volume*). Tokenit tallentuvat sinne ja säilyvät
   uudelleenkäynnistysten ja uusien deployjen yli.

## 2. Kirjaudu Garminiin

Avaa selaimessa:

```
https://<domain>/login?key=<MCP_AUTH_TOKEN>
```

Syötä Garmin-sähköposti ja salasana, ja sen jälkeen vahvistuskoodi, jos Garmin
pyytää sitä. Onnistuneen kirjautumisen jälkeen MCP-työkalut toimivat heti, eikä
palvelua tarvitse käynnistää uudelleen. Salasanaa ei tallenneta, vain
kirjautumistokenit, jotka ovat voimassa noin 6 kuukautta. Kun ne vanhenevat,
kirjaudu samalla sivulla uudelleen.

`https://<domain>/readyz` palauttaa `{"status": "ready"}`, kun kirjautuminen on
voimassa.

**Ilman Volumea** tokenit katoavat jokaisessa uudelleenkäynnistyksessä. Silloin
kopioi onnistumissivun kohta *Näytä GARMIN_TOKENS_BASE64* Railwayn muuttujaan
`GARMIN_TOKENS_BASE64`, niin palvelin kirjautuu sillä käynnistyessään.

## 3. Yhdistä MCP-asiakas

MCP-endpoint: `https://<domain>/mcp`

- **claude.ai (Custom connector)**: URL muodossa
  `https://<domain>/mcp?key=<MCP_AUTH_TOKEN>`
- **Claude Code**:
  ```bash
  claude mcp add --transport http garmin https://<domain>/mcp \
    --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
  ```

Pidä `MCP_AUTH_TOKEN` salassa — sillä pääsee lukemaan ja muokkaamaan
Garmin-tiliäsi ja vaihtamaan palvelimen kirjautuneen tilin.

## Vaihtoehto: tokenit omalta koneelta

```bash
uvx --python 3.12 --from git+https://github.com/OJHela/Garmin-mcp garmin-mcp-auth
cat ~/.garminconnect_base64
```

Liitä tulostettu merkkijono Railwayn muuttujaan `GARMIN_TOKENS_BASE64`.
