# Garmin MCP Railwayssä

Tämä repo on kopio projektista [taxuspt/garmin_mcp](https://github.com/taxuspt/garmin_mcp)
(MIT-lisenssi), johon on lisätty Railway-hostaus ja kirjautuminen
**kirjautumisikkunalla** (OAuth): kun lisäät yhteyden Claudeen, aukeaa ikkuna,
jossa kirjaudut Garmin-tunnuksillasi. Sen jälkeen Claude hakee tietosi MCP:n kautta.

## Käyttöönotto

1. Railway: **Deploy from GitHub repo** → `OJHela/Garmin-mcp`, ja
   **Settings → Networking → Generate Domain**. Muuta ei tarvita.
2. Claude: **Asetukset → Connectors → Add custom connector**
   - URL: `https://<domain>/mcp`
   - Authentication: **Sign in now**
   - OAuth client: **Register automatically**
3. Paina **Add / Connect**. Aukeaa Garmin-kirjautumisikkuna: syötä sähköposti,
   salasana ja tarvittaessa Garminin lähettämä vahvistuskoodi.
4. Valmis. Garmin-työkalut toimivat keskusteluissa.

## Miten se toimii

- Palvelin on oma OAuth 2.1 -palvelimensa (dynaaminen asiakasrekisteröinti, PKCE).
  `/mcp` hyväksyy vain kirjautumisessa saadun tokenin, joten palvelin ei ole
  avoin, vaikka et asettaisi mitään muuttujia.
- Jokainen kirjautuminen on oma istuntonsa: jokainen käyttäjä näkee vain oman
  Garmin-tilinsä tiedot.
- Salasanaa ei tallenneta eikä se kulje keskustelun kautta, vain
  Garmin-kirjautumistokenit (voimassa noin 6 kk).
- Osoite päätellään Railwayn `RAILWAY_PUBLIC_DOMAIN`-muuttujasta. Omalla
  domainilla aseta `GARMIN_MCP_PUBLIC_URL=https://oma.domain`.

**Uudelleenkäynnistykset:** ilman Volumea istunnot katoavat, kun Railway korvaa
kontin (esim. uusi deploy). Silloin Claude pyytää kirjautumaan uudelleen samassa
ikkunassa. Tämän välttää lisäämällä Volumen polkuun `/root/.garminconnect`.

## Muut tilat

- `GARMIN_MCP_OAUTH=false`: ei kirjautumisikkunaa. Tällöin kirjautuminen
  tehdään työkaluilla `garmin_login` / `garmin_submit_mfa_code` keskustelussa,
  ja endpointin voi suojata `MCP_AUTH_TOKEN`-muuttujalla (`Authorization:
  Bearer <token>` tai `?key=<token>`) sekä käyttää selainsivua
  `/login?key=<token>`.
- `GARMIN_TOKENS_BASE64`: tokenit ympäristömuuttujana (`garmin-mcp-auth`
  omalla koneella, sitten `cat ~/.garminconnect_base64`).
