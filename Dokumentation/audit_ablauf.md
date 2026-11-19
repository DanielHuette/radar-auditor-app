---
Art: Anleitung
Zweck: Ablauf eines Audits des RadarAuditor von der Handy-App bis zur Auswertung – ausgeführt vom Audit-Agenten, mit jeder Messung (Quelle, Kosten, Prüfungen)
Stand: Eingang
Version: 4
Aufruf: 
---

# Ablauf eines Audits im RadarAuditor

Leinwand: [[20_Firma_RepoCity/28_Produkte/281_RadarAuditor/Dokumentation/audit_ablauf.canvas]]. Stand 15.11.2026.

Farben der Leinwand: grün Station, blau Messung, violett Regel für das ganze Audit.

## Ablauf in einer Zeile

Handy-App → Echtzeit-Signal weckt den Audit-Agenten → Vorbereitung → Kostenvoranschlag → Probeabruf je bezahltem Messweg → alle Messungen → Bewerten → Kontrolle je Prüfung → Auswertung und Handlungsempfehlung → Bericht als PDF → Selbstprüfung → Meldung aufs Handy → Auswertung in der App.

## Grundsatz

Ein Audit führt der Audit-Agent (Claude) vollständig selbst aus. Er ruft jeden Messdienst selbst auf, sieht jede Antwort selbst an, bewertet jede Prüfung selbst und schreibt Auswertung und Handlungsempfehlung selbst. Programme übernehmen nur Handgriffe ohne Urteil: das Wecken des Agenten, den Druck des Berichts als PDF, das Senden der Handy-Meldung.

## Regeln für das ganze Audit

- **Hält nie an, wartet nie auf Daniel.** Fällt ein Messweg aus, wird er mit Grund als fehlgeschlagen vermerkt, seine Prüfungen stehen als „nicht gemessen“ mit diesem Grund im Ergebnis, das Audit läuft weiter. Einen Fehler behebt der Agent an der Ursache.
- **Keine Messung weglassen, keine Daten erfinden, keinen Fehler verschweigen.**
- **Ausfallsicherung: nur der gescheiterte Schritt startet neu.** Jedes Teilergebnis wird gespeichert, sobald es fertig ist. Erledigte Schritte werden nie wiederholt und nie neu bezahlt, auch nicht nach einem Neustart des Rechners oder Servers.
- **Gescheiterte Teile werden neu gestartet.** Scheitert ein Teil (eine Messung, das Bewerten, der Bericht, eine Meldung), läuft das Audit mit allen anderen Teilen weiter. Der gescheiterte Teil startet sofort neu: erst derselbe Weg, dann der zweite Weg. Scheitert er weiter, übernimmt der Audit-Agent genau diesen Teil: Er bekommt Fehlermeldung und Rohantwort, sucht die Ursache, führt den Teil neu aus und lässt die betroffenen Prüfungen neu bewerten. Aufgegeben wird nicht: Der Agent arbeitet an dem Teil weiter, bis er funktioniert. Nach 5 Fehlversuchen desselben Teils geht eine Warnmeldung an die Cockpit-App, danach geht die Arbeit weiter. Bezahlte Neuversuche sind auf 3 je Schritt begrenzt. Die Schritte der bezahlten Sichtbarkeit (Google Ads, Meta Ads) blockieren das Audit nicht: Scheitern sie, sind p1 und p2 „nicht gemessen“ mit Grund. Fehlt vor dem Start eine Datenbank-Änderung, bricht das Audit vorher mit einer deutschen Meldung ab. Ein bezahlter Neuversuch erfolgt nur, nachdem der Agent etwas an der Ursache geändert hat, zuerst als einzelner Probeabruf, und erst nach dem Lesen des Guthabens. Reicht das Guthaben nicht, geht eine Warnmeldung mit Fehlbetrag an die Cockpit-App, der Teil wartet auf Guthaben, alles andere läuft weiter. Auswertung, Bericht und Schlussmeldung entstehen, sobald der Teil erledigt ist. Jeder Neustart steht als Ereignis in App und Cockpit.
- **Kein Geld ohne Probe.** Jeder bezahlte Messweg wird zuerst mit genau einem Abruf geprüft. Der Agent sieht die Antwort selbst an. Ist sie unbrauchbar, wird dieser Weg nicht weiter bezahlt und als fehlgeschlagen mit Grund vermerkt.
- **Kein Abbruch wegen Geld.** Die Kosten werden vor dem Start hochgerechnet und mit dem Guthaben verglichen. Reicht es nicht, startet das Audit nicht, und Daniel bekommt eine Meldung mit dem fehlenden Betrag.
- **Jede Rohantwort wird aufbewahrt.** Alle Antworten der Messdienste liegen unverändert im Ordner des Audits (Speicher `raw`, Unterordner je Audit). Jede Zahl im Bericht lässt sich darauf zurückführen.
- **Keine Dauerabfrage.** Der Agent startet einmal, wenn ein Audit in der App gestartet wird.
- **DataForSEO ist erster Weg** für Google-Suche, Google Maps und ChatGPT, immer in der schnellen Warteschlange (Ergebnis in bis zu 5 Minuten). Jede Antwort ist dort 30 Tage lang kostenlos wieder abrufbar.
- **So schnell wie möglich.** Alle Messungen, die nicht aufeinander warten müssen, laufen gleichzeitig. Es gibt keine eingebauten Pausen. Gewartet wird nur, wo ein Dienst es erzwingt (Begrenzung der Abrufe je Minute) oder eine Antwort noch nicht fertig ist. Fertige Antworten holt der Agent sofort ab.
- **Zwei Abfragen derselben Suche können unterschiedlich ausfallen.** Der Agent nennt im Bericht den Zeitpunkt jeder Abfrage.

## Stationen

### 1 · Start in der Handy-App

- Seite Prüfen: Webseite-Adresse eintragen, Prüfungen an- oder abwählen.
- Datenbankfunktion `start_field_audit` legt Kunde, Standort, Suchbegriffe, Wettbewerber und das Audit an.
- Fortschritt in der App über das Echtzeit-Abo auf `audit_events`.

### 2 · Wecken

- Das neue Audit löst ein Echtzeit-Signal aus. Ein kleiner Weckdienst startet darauf den Agenten einmal mit der Audit-Nummer.
- Der Agent läuft über Claude Code mit dem Jahresschlüssel aus `claude setup-token`. Zwei Wochen vor Ablauf kommt eine Erinnerung.
- Fällt der Weckdienst aus, startet er sich selbst neu. Meldungen wie „Sammler weg/wieder da“ gibt es nicht.

### 3 · Vorbereitung

- Webseite lesen: bis 60 Seiten, `robots.txt` beachten. Titel, Beschreibung, Überschriften, Strukturdaten, Links, Bilder, Impressum, Datenschutz, Kontakt, Sitemap, eine Fehlerseite.
- Betrieb erkennen: Name, Anschrift, Telefon, Branche aus Impressum, Strukturdaten und Fußzeile. Fehlende Koordinaten aus Straße, Postleitzahl und Ort über OpenStreetMap.
- Suchbegriffe: die Begriffe des Kunden, getrennt in örtlich (mit Ort) und bundesweit, dazu der Betriebsname.
- Eigene Konten in sozialen Netzwerken: zuerst die Links auf der Webseite und im Impressum.

### 4 · Kostenvoranschlag

- Der Agent rechnet jeden geplanten bezahlten Abruf mit den Preisen unten durch und speichert ihn in `audit_estimates`.
- Guthaben lesen (kostenlos): DataForSEO `appendix/user_data`, Apify Monatsrest.
- Reicht das Guthaben: weiter. Reicht es nicht: kein Start, Meldung aufs Handy mit Fehlbetrag. Als eigenes PDF in der App abrufbar.

### 5 · Probeabruf je bezahltem Messweg

- Je bezahltem Weg genau ein Abruf mit dem Betriebsnamen oder dem Hauptsuchbegriff.
- Der Agent prüft die Antwort selbst: Kommt die echte Webseite der Treffer vor, nicht nur Weiterleitungen? Sind die Felder gefüllt? Passt die Antwort zur Frage?
- Unbrauchbar: Weg nicht weiter bezahlen, fehlgeschlagen mit Grund. Die übrigen Wege laufen weiter.

### 6 · Messungen

Alle Messungen im Einzelnen stehen unten. Alle laufen gleichzeitig an; nur Verlinkungen und Mitbewerber-Lücke warten auf die Wettbewerber aus dem Google-Profil. Jede Rohantwort wird im Ordner des Audits gespeichert.

### 7 · Bewerten

- 62 Prüfungen, nach Anbietern geordnet (Google 29, Bing 18, Apple 1, OpenAI 4, Anthropic 4, Google Ads 4, Meta Ads 2) aus `config\pruefkatalog.yaml`. Der Agent bewertet jede Prüfung anhand der Rohantworten und der Schwellen im Katalog: bestanden, teils, nicht bestanden, nicht gemessen mit Grund.
- Punkte: bestanden 1, teils 0,5, nicht bestanden 0. Nicht gemessene Prüfungen stehen als Zeile mit Gewicht 0 im Ergebnis. Gesamtpunkte 0 bis 100; zählt keine Prüfung, bleibt der Gesamtwert leer mit Grund.
- Geplant nicht messbar (keine Kontrolle): m1, m2, p5, p6 (kein Zugang oder bezahlte Messung ausgeschaltet), j9 beim ersten Audit.
- Ergebnisse in `check_results`, ein Ereignis je Prüfung in `audit_events`.

### 7a · Kontrolle je Prüfung

- Je Prüfung ein Kontrollpaket: Auftrag, Prüfungsdaten (ohne stilles Kürzen, bei Überlänge in Teilen), Regel in Alltagssprache, errechnetes Ergebnis.
- Drei Urteile mit Beleg „Feld: Wert“ aus den Prüfungsdaten: Daten korrekt eingeholt, Daten korrekt, Auswertung korrekt. Ein Haken ohne Beleg wird zum X.
- Drei Fälle: geplant nicht messbar (keine Kontrolle, Balken „fertig“, neutral); Messung gescheitert (nur das erste Urteil, „nicht gemessen“ mit Grund); gemessen (alle drei).
- Ein X nimmt die Prüfung aus Gesamtwert und Anbieterwerten. Eine Neubewertung setzt die Kontrolle zurück.
- Fällt der Kontrollagent aus, wartet er 10, 30, 60, dann je 300 Sekunden. Die Kontrolle jedes Audits läuft unabhängig und endet nie von selbst.

### 8 · Auswertung und Handlungsempfehlung

- Der Agent schreibt die Auswertung in klaren Worten: das Wichtigste in Kürze, was schon gut ist, den Abschnitt „Bezahlte Sichtbarkeit“, die Kontrolltabelle mit Ergebnis und amtlicher Quelle je Prüfung. Die Handlungsempfehlung entsteht nur aus Prüfungen mit drei Haken, je Prüfung eine Maßnahme, sortiert nach Wirkung (Priorität 1 bis 3).
- Er prüft jeden Vorschlag gegen die Branche des Kunden. Unpassende Ergebnisse (zum Beispiel ein Mitbewerber aus einer anderen Branche) nennt er als solche und baut keine Empfehlung darauf.
- Ein Abschnitt nennt alles, was nicht oder nur teilweise gemessen werden konnte, mit Grund.
- Kostenaufstellung aller Dienste des Audits.

### 9 · Bericht als PDF

- Das Programm baut beide PDF-Dateien selbst aus den gespeicherten, kontrollierten Messungen (`radarauditor report sales`); es gibt nur diesen Weg, keine von Hand geschriebene Seite und keinen Chrome-Druck.
- Aussagen, Zahlen und Diagramme stehen nur für Prüfungen mit drei Haken; es gibt keine Prognose.
- Neben dem Gesamtwert, dem Wert je Anbieter und dem Wert je Bereich steht, aus wie vielen der wertbaren Prüfungen er stammt ("aus 12 von 56 Prüfungen"). Eine Mindestmenge gibt es nicht; ob es eine geben soll, entscheidet Daniel.
- Die Kontrolle prüft je Messschritt einmal, ob die Daten korrekt eingeholt wurden, gegen die unveränderten Antworten aller Lieferanten des Schritts (in so vielen Teilen wie nötig); das Urteil gilt für alle Prüfungen des Schritts. Für "Daten korrekt" und "Auswertung korrekt" bekommt jede Prüfung je Suchbegriff den unveränderten Trefferteil der Antwort.
- k1, k2, k6, k7 und j3 haben keinen Grenzwert, kein Urteil und keine Punkte; sie zeigen den Messwert und denselben Wert der Wettbewerber aus demselben Audit.
- Zwei Dateien direkt im Projektordner: `Auswertung_<Webseite>.pdf` und `Empfehlung_<Webseite>.pdf`, zum Beispiel `Auswertung_katjakarsch.de.pdf`; ist der Projektordner nicht beschreibbar, liegen sie unter `berichte/<Audit-ID>/` im Arbeitsordner. Die Empfehlung nennt je Maßnahme die Zeile „Gemessen“. Ungültige Messungen nennen den Grund im Klartext, Kosten tragen Alltagsnamen.
- Dazu Ablage im Speicher `reports` für die App, Status `reported`.

### 10 · Selbstprüfung und Urteil

- Der Agent gleicht Stichproben jeder Messung mit den Rohantworten ab: stimmt jede Zahl im Bericht?
- Urteil: vollständig, unvollständig (mit Lücken und Grund) oder fehlgeschlagen. Als Ereignis „Audit-Agent: Ergebnis“ in `audit_events`.

### 11 · Meldung aufs Handy

- Über ntfy (Thema `NTFY_TOPIC` aus der zentralen `.env`): Audit gestartet, Messweg fehlgeschlagen, fertig mit Urteil und Punkten. Jede Meldung trägt den Link zur Auswertung. Auch ins Cockpit.

### 12 · Auswertung in der App

- Urteil des Agenten, jede Messung mit Ergebnis und Grund, Gesamtpunkte, Bereiche, alle Prüfungen mit Messwerten, nicht gemessene Prüfungen mit Grund, Handlungsempfehlung, Suchbegriffe örtlich und bundesweit, soziale Netzwerke (eine Zeile je Konto), Wettbewerber, Kostenvoranschlag als eigenes PDF, Kostenaufstellung, Bericht zum Herunterladen.
- Alles, was das Cockpit zum Audit zeigt, steht auch hier.

## Messungen im Einzelnen

62 Prüfungen. Schritte und Zuordnung stehen in `config\schritte.json`, die Anbieter im Prüfkatalog.

| Messung | Quelle | Kosten je Abruf | Prüfungen |
|---|---|---|---|
| Webseite (crawler, Schritte website und company) | Webseite selbst, bis 60 Seiten | 0 | a4 a7 a11 a12 a13 a14 a15 a16 a17 a18 b3 b4 b5 b10 b11 b12 b13 b14 b15 b16 b17 b18 b19 b20 b21 b22 c4 |
| PageSpeed | PageSpeed Insights mobil und Desktop | 0 | a2 |
| Google-Profil (places) | DataForSEO `serp/google/maps`, `business_data/google/my_business_info`, Fragen, Bewertungen; Google Maps im Browser und Places-Schnittstelle als zweiter Weg | Kartenseite 0,0012 USD; Profil 0,0015 USD; Fragen 0,0015 USD je 20; Bewertungen 0,0015 USD je 10 | f1 f2 f4 f5 f7 f8 f13 g1 g2 j3 |
| Google-Suche (serp) | DataForSEO `serp/google/organic`, 3 Ergebnisseiten je Begriff; Apify `apify~google-search-scraper` als zweiter Weg | 0,0012 USD erste Seite, jede weitere 0,0009 USD | j1 j2 j4 j6 j9 p3 p4 |
| Bing-Suche (serp, search_bing) | Bing im Browser, Seite wird gespeichert | 0 | j7 |
| Bildersuche (serp, search_images) | Apify `johnvc~google-images-api`, 40 Bilder je Hauptbegriff | 0,004 USD je Begriff | j8 |
| Verweise (paid, links) | DataForSEO Backlinks und Labs | rund 0,02 bis 0,13 USD je Anfrage | i1 j5 c12 |
| KI-Assistenten (assistants) | ChatGPT über DataForSEO `ai_optimization/chat_gpt/llm_scraper`, Claude über Abonnement; Fragen aus `config\fragen.yaml`, je 10 Mal | ChatGPT 0,0024 USD je Antwort | k1 k2 k6 k7 |
| Verzeichnisse (directories) | Apple Karten über Apify `zen-studio~apple-maps-scraper-pro`, Bing Karten über `karamelo~bing-maps-scraper` | 0,02 USD + 0,004 USD je Ort; 0,005 USD + 0,0005 USD je Ergebnis | h1 h2 |
| Google Ads (ads_google) | Werbebibliothek und Anzeigen zum Namen | je nach Weg | p1 |
| Meta Ads (ads_meta) | Meta-Werbebibliothek | je nach Weg | p2 |
| Zugang (access) | nur mit Kundenzugang, geplant nicht messbar | 0 | m1 m2 p5 p6 |
### Webseite und Technik

1. Seiten abrufen, beginnend bei der Startseite, bis 60 Seiten der eigenen Domain.
2. Jede Seite ansehen: Titel, Beschreibung, Überschriften, Strukturdaten, Links, Bilder, Skripte, Fußzeile, Impressum, Datenschutz, Fehlerseite.
3. PageSpeed mobil und Desktop.
4. Wayback Machine: erste Aufnahme der Domain.

### Google-Suche

1. Probeabruf mit dem Betriebsnamen.
2. Alle Suchbegriffe über DataForSEO, 3 Ergebnisseiten je Begriff, Standort des Kunden.
3. Die echte Adresse eines Treffers steht in der angezeigten Adresse (`breadcrumb`, `website_name`), nicht im Link, der eine Weiterleitung über google.de ist.
4. Platz der eigenen Webseite je Begriff. Konten in sozialen Netzwerken zählen nicht als eigene Webseite.
5. Aus derselben Antwort: Kartenpaket, KI-Übersicht, „Weitere Fragen“, Anzeigen.
6. Begriffe ohne Antwort über Apify.

### Bing-Suche

1. Bing im Browser: nur die Suchseite öffnen, keine Abfrage zu Standardbrowser oder Standardsuche bestätigen, Cookie-Abfrage nicht annehmen, Seite speichern.
2. Hauptsuchbegriffe und Betriebsname, Platz der eigenen Webseite lesen.

### Google-Unternehmensprofil und Bewertungen

1. Kartensuche je Suchbegriff über DataForSEO: Platz des Betriebs, die ersten Betriebe der Karte.
2. Raster: dieselbe Kartensuche an Punkten rund um den Standort (Koordinaten im Auftrag).
3. Profil über DataForSEO: Kategorien, Beschreibung, Öffnungszeiten, Fotos, Beiträge, Angaben.
4. Bewertungen mit Antworten des Inhabers und Fragen im Profil über DataForSEO.
5. Bis zu 3 Wettbewerber aus derselben Branche, aus der Kartensuche.
6. Kein Profil ist ein Befund, kein Ausfall.
7. Zweiter Weg: Google Maps im Browser, dann Places-Schnittstelle.

### Verlinkungen, Mitbewerber, Verlauf

1. Für Kunde und bis zu 3 Wettbewerber: verweisende Domains, Zahl der Begriffe mit Platzierung.
2. Mitbewerber-Lücke: Begriffe, bei denen ein Wettbewerber gefunden wird und der Kunde nicht. Der Agent prüft, ob der Wettbewerber zur Branche passt.
3. Verlauf über 12 Monate.

### Verzeichnisse, Karten, Portale

1. Gelbe Seiten, Das Örtliche, 11880 sind keine eigenen Prüfungen mehr; ein Eintrag ohne Treffer ist ein Befund, kein Ausfall.
2. Apple Karten und Bing Karten über Apify, bei jedem Audit.
3. Wikidata nach Name und Webseite.
4. Fachportale und Bewertungsportale je Branche über die Google-Seitensuche (`config\verzeichnisse.yaml`).
5. Gefundene Einträge: Name, Anschrift, Telefon mit der Webseite vergleichen.

### KI-Assistenten

1. Fragen aus `config\fragen.yaml`: 3 Empfehlungsfragen, 3 Problemfragen, je 10 Mal, örtlich und bundesweit.
2. ChatGPT und Google KI-Modus über DataForSEO, Claude direkt. Zweiter Weg für ChatGPT: Browser.
3. Je Antwort: Betrieb genannt, genannte Quellen, Auszug.

### Soziale Netzwerke

1. Konten aus den Links der Webseite und des Impressums, sonst Google-Suche je Plattform mit bis zu 3 Namensvarianten.
2. Bestätigt, wenn das Konto auf die Webseite verlinkt, die Telefonnummer passt oder Name und Ort zusammen stehen.
3. Je Konto: Follower, letzter Beitrag, Beiträge in 90 Tagen, Reaktionsquote.
4. Kein Konto nach echter Suche ist ein Befund.

### Wirkung (nur mit Kundenzugang)

1. Google-Profil-Kennzahlen, Search Console, Analytics, Bewertungen mit Antworten.
2. Ohne Zugang: m1 und m2 geplant nicht messbar („nur mit Zugang zu Ihren Google-Konten messbar“), ebenso p5 und p6 ohne Kundenzugang zu den Werbekonten.
