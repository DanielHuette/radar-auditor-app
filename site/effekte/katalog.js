export const bereiche = [
  { id: "google", gewicht: 56.179, de: "Google", en: "Google",
    pruefungen: [
      ["Die Seite lädt am Handy schnell und ruhig", "Page loads quickly and steadily on a phone", "Lädt die Seite am Handy schnell, ohne zu springen, und reagiert sie sofort auf Eingaben?", "Jede Sekunde mehr kostet Anrufe, weil Besucher abspringen; Google stuft langsame Seiten zurück.", "a2"],
      ["Google kann jede Seite aufnehmen", "Google can include every page", "Darf Google jede Seite Ihrer Website abrufen und in die Suche aufnehmen?", "Eine Seite, die Google nicht abrufen darf, nicht fehlerfrei erhält oder mit „nicht aufnehmen“ oder dem Verweis auf eine andere Adresse versehen ist, erscheint nicht in der Suche und bringt keine Kunden.", "a4"],
      ["Der Vorschautext ist nicht gesperrt", "The preview text is not blocked", "Darf Google zu den Seiten einen Vorschautext zeigen?", "Seiten mit gesperrtem Vorschautext werden in Googles Antworten mit künstlicher Intelligenz nicht als Quelle genannt.", "a12"],
      ["Googles künstliche Intelligenz (Gemini) darf die Inhalte verwenden", "Google's AI (Gemini) may use the content", "Darf Gemini die Inhalte der Website verwenden?", "Bei einer Sperre taucht die Website in Gemini nicht als Quelle auf; die Google-Suche bleibt unberührt.", "a11"],
      ["Google sieht dieselbe Seite wie Besucher", "Google sees the same page as visitors", "Zeigt die Website Google und Besuchern denselben Inhalt?", "Unterschiedliche Inhalte für Google und Besucher können die Seite aus der Suche werfen.", "a13"],
      ["Die Bilder sind beschrieben", "Images are described", "Haben die Bilder der Website eine Bildbeschreibung?", "Ohne Bildbeschreibung versteht Google nicht, was ein Bild zeigt, und zeigt es seltener.", "a7"],
      ["Jede Seite ist über einen lesbaren Link erreichbar", "Every page is reachable through a readable link", "Ist jede Seite über einen echten Link erreichbar?", "Seiten, auf die kein gewöhnlicher Link zeigt, findet Google nicht und zeigt sie nicht in der Suche.", "b5"],
      ["Die Suchwörter der Kunden stehen an den auffälligen Stellen", "Customers' search words appear in the prominent places", "Stehen die Suchwörter der Kunden an den Stellen, die Google liest?", "Google findet nur, was auch dasteht; fehlen die Suchwörter, erscheint die Seite bei diesen Suchen nicht.", "c4"],
      ["Verweise von anderen Websites im Vergleich zu den Wettbewerbern", "Links from other websites compared with the competitors", "Verweisen so viele andere Websites auf den Betrieb wie auf die Wettbewerber?", "Links sind Empfehlungen; Google zählt sie.", "i1"],
      ["Das Profil ist bestätigt", "The profile is verified", "Hat der Betrieb sein Google-Profil in der Hand?", "Ohne Profil keine Karte; ohne Besitz kann jeder die Daten ändern.", "f1"],
      ["Die Hauptkategorie ist gesetzt", "The main category is set", "Ist im Google-Profil eine Hauptkategorie gesetzt?", "Die Kategorie entscheidet, bei welchen Suchen der Betrieb auf der Karte erscheint.", "f2"],
      ["Die Beschreibung ist vorhanden", "The description is present", "Hat das Google-Profil eine Beschreibung?", "Ohne Beschreibung bleibt offen, wofür der Betrieb steht.", "f4"],
      ["Die Leistungen sind eingetragen", "The services are entered", "Sind die Leistungen im Google-Profil eingetragen?", "Fehlen die Leistungen, wird der Betrieb bei Suchen nach einzelnen Leistungen seltener gezeigt.", "f5"],
      ["Die Öffnungszeiten sind vollständig", "Opening hours are complete", "Sind die Öffnungszeiten vollständig eingetragen, soweit erkennbar auch für besondere Tage?", "Falsche oder fehlende Zeiten kosten Anrufe und Besuche.", "f7"],
      ["Fotos sind vorhanden", "Photos are present", "Hat das Google-Profil Fotos?", "Ohne Fotos wirkt der Eintrag leer und wird seltener angeklickt.", "f8"],
      ["Die Merkmale sind eingetragen", "The attributes are entered", "Sind Merkmale im Google-Profil eingetragen?", "Ohne Merkmale fehlen Kunden Angaben, die sie vor dem Besuch suchen.", "f13"],
      ["Zahl der Rezensionen im Vergleich zu den Betrieben auf der Karte", "Number of reviews compared with the businesses on the map", "Hat der Betrieb mehr oder weniger Bewertungen als die Wettbewerber?", "Kunden wählen den mit mehr Bewertungen, Google auch.", "g1"],
      ["Durchschnittsbewertung im Vergleich zu den Betrieben auf der Karte", "Average rating compared with the businesses on the map", "Liegt die Durchschnittsbewertung mindestens auf dem Niveau der Betriebe auf der Karte?", "Schlechtere Sterne als bei den Wettbewerbern kosten Anfragen.", "g2"],
      ["Platz bei den wichtigsten Suchen im eigenen Ort", "Position for the most important searches in the town", "Auf welchem Platz steht der Betrieb für die wichtigsten Suchen?", "Die obersten Plätze bekommen die meisten Klicks, ab Seite 2 fast niemand mehr.", "j1"],
      ["Unter den drei Betrieben auf der Karte", "Among the three businesses on the map", "Ist der Betrieb einer der drei Betriebe auf der Karte?", "Die Karte steht über allen Ergebnissen; wer dort fehlt, wird am Handy kaum gesehen.", "j2"],
      ["Sichtbarkeit rund um den Standort", "Visibility around the location", "Wo im Umkreis ist der Betrieb auf der Karte sichtbar und wo nicht?", "Zeigt, aus welchen Stadtteilen keine Kunden kommen können.", "j3"],
      ["Platz 1 bei der Suche nach dem eigenen Namen", "Position 1 when searching for the business's own name", "Findet man den Betrieb, wenn man seinen Namen sucht?", "Wer nach dem Betrieb sucht, hat sich fast entschieden; da darf nichts dazwischen stehen.", "j4"],
      ["Anzahl der Suchen, bei denen der Betrieb gefunden wird", "Number of searches for which the business is found", "Für wie viele Suchen wird der Betrieb überhaupt gefunden?", "Mehr Begriffe, mehr Türen.", "j5"],
      ["Genannt in Googles Antwort mit künstlicher Intelligenz", "Named in Google's AI answer", "Nennt Googles Antwort mit künstlicher Intelligenz den Betrieb?", "Diese Antwort steht ganz oben und verdrängt die normalen Ergebnisse.", "j6"],
      ["Eigene Bilder in der Google-Bildersuche", "Own images in Google image search", "Erscheinen die Bilder des Betriebs bei Google Bilder?", "Für Handwerk und Gastronomie ein eigener Kundenweg.", "j8"],
      ["Suchbegriffe, bei denen der Wettbewerber gefunden wird und der eigene Betrieb nicht", "Search terms where the competitor is found and the business is not", "Für welche Begriffe wird der Wettbewerber gefunden und der eigene Betrieb nicht?", "Das ist die Liste der Aufträge, die heute woanders landen.", "c12"],
      ["Entwicklung über die Zeit", "Development over time", "Steigt die Sichtbarkeit von Monat zu Monat?", "Zeigt, ob die laufende Betreuung wirkt.", "j9"],
      ["Anrufe, Routen und Aufrufe aus dem Google-Profil", "Calls, routes and views from the Google profile", "Wie viele Menschen rufen den Betrieb über Google an?", "Die Zahl, an der sich jede Maßnahme messen lässt.", "m1"],
      ["Suchwörter, bei denen wirklich geklickt wird", "Search words that actually get clicks", "Wofür wird der Betrieb wirklich geklickt?", "Ersetzt jede Schätzung durch Messung.", "m2"]
    ] },
  { id: "bing", gewicht: 29.214, de: "Bing und Copilot", en: "Bing and Copilot",
    pruefungen: [
      ["Bing kann jede Seite aufnehmen", "Bing can include every page", "Darf Bing jede Seite der Website abrufen und aufnehmen?", "Seiten, die Bing nicht aufnimmt, erscheinen weder bei Bing noch in Copilot.", "a14"],
      ["Keine Sperrvermerke für Copilot", "No blocking notes for Copilot", "Verbietet die Website Copilot, ihre Inhalte zu zitieren?", "Gesperrte Seiten werden in Antworten von Copilot nicht oder nur eingeschränkt genannt.", "b10"],
      ["Das Seitenverzeichnis (Sitemap) ist vorhanden und sauber", "The sitemap is present and clean", "Gibt es ein vollständiges, sauberes Seitenverzeichnis?", "Ein fehlendes oder fehlerhaftes Verzeichnis lässt Bing Seiten spät oder gar nicht finden.", "b11"],
      ["Der Meldedienst IndexNow ist eingerichtet", "The notification service IndexNow is set up", "Meldet die Website Änderungen automatisch an Bing?", "Ohne Meldung erfährt Bing von Änderungen erst beim nächsten Besuch.", "b12"],
      ["Links sind lesbar und beschriftet", "Links are readable and labelled", "Sind die Links der Website lesbar und beschriftet?", "Links ohne Beschriftung sagen Bing nichts über die Zielseite.", "b13"],
      ["Keine doppelten Adressen", "No duplicate addresses", "Ist jede Seite nur unter einer Adresse erreichbar?", "Doppelte Adressen teilen die Kraft einer Seite auf und verwirren Bing.", "b14"],
      ["Der Inhalt ist ohne Skripte lesbar", "The content is readable without scripts", "Steht der wichtige Text direkt im Seitentext?", "Was erst durch Skripte erscheint, wird von Suchmaschinen leicht übersehen.", "b15"],
      ["Seitentitel und Vorschautext sind je Seite vorhanden und nicht doppelt", "Page title and preview text are present on every page and not duplicated", "Hat jede Seite einen eigenen Titel und Vorschautext?", "Fehlende oder doppelte Texte machen Treffer unklar und bringen weniger Klicks.", "b3"],
      ["Die Überschriften sind logisch gegliedert", "The headings are logically structured", "Sind die Überschriften jeder Seite logisch gegliedert?", "Eine unklare Gliederung erschwert es Bing, den Inhalt einzuordnen.", "b4"],
      ["Bilder sind beschrieben und sprechend benannt", "Images are described and sensibly named", "Haben die Bilder eine Beschreibung und einen sprechenden Dateinamen?", "Ohne beides bleibt der Inhalt der Bilder für Bing unklar.", "b16"],
      ["Jede Seite behandelt ein Thema", "Each page covers one topic", "Nennen Titel, Hauptüberschrift und Text dasselbe Thema?", "Seiten ohne klares Thema werden bei Suchen seltener gezeigt.", "b17"],
      ["Wichtiges steht oben", "What matters comes first", "Stehen Leistung und Ort gleich am Seitenanfang?", "Was erst weit unten steht, gewichtet Bing geringer.", "b18"],
      ["Die Inhalte sind aktuell", "The content is up to date", "Zeigen die Änderungsdaten der Seiten, dass die Inhalte gepflegt werden?", "Veraltete Inhalte werden seltener gezeigt.", "b19"],
      ["Angaben im Seitencode stimmen mit dem sichtbaren Text überein", "Data in the page code matches the visible text", "Stimmen die maschinenlesbaren Angaben mit dem sichtbaren Text überein?", "Widersprüche machen die Angaben für Bing unzuverlässig.", "b20"],
      ["Der Firmenname ist einheitlich geschrieben", "The business name is written consistently", "Ist der Firmenname überall gleich geschrieben?", "Verschiedene Schreibweisen erschweren es, den Betrieb eindeutig zu erkennen.", "b21"],
      ["Bing sieht dieselbe Seite wie Besucher", "Bing sees the same page as visitors", "Zeigt die Website Bing und Besuchern denselben Inhalt?", "Unterschiedliche Inhalte können zum Ausschluss aus Bing führen.", "b22"],
      ["Gefunden bei Bing", "Found on Bing", "Wird der Betrieb auch bei Bing gefunden?", "Bing speist Copilot und ChatGPT-Suche.", "j7"],
      ["Eintrag bei Bing", "Listing on Bing", "Findet man den Betrieb bei Bing?", "Windows-Nutzer und der Assistent von Microsoft suchen hier.", "h2"]
    ] },
  { id: "apple", gewicht: 1.623, de: "Apple Karten", en: "Apple Maps",
    pruefungen: [
      ["Eintrag in Apple Karten", "Listing in Apple Maps", "Findet man den Betrieb auf dem iPhone in Apple Karten?", "Viele iPhone-Nutzer suchen dort statt bei Google.", "h1"]
    ] },
  { id: "openai", gewicht: 6.492, de: "ChatGPT", en: "ChatGPT",
    pruefungen: [
      ["Die ChatGPT-Suche darf die Website lesen", "The ChatGPT search may read the website", "Erlaubt die robots.txt dem Abrufprogramm der ChatGPT-Suche (OAI-SearchBot) das Lesen der Website?", "Ein gesperrter OAI-SearchBot lässt den Betrieb in der ChatGPT-Suche aus.", "a15"],
      ["Die Firewall lässt die ChatGPT-Suche durch", "The firewall lets the ChatGPT search through", "Kommt OAI-SearchBot an der Firewall der Website vorbei?", "Eine blockierende Firewall sperrt die ChatGPT-Suche aus, obwohl die robots.txt es erlaubt.", "a16"],
      ["Genannt, wenn Kunden ChatGPT nach einem Betrieb fragen", "Named when customers ask ChatGPT about a business", "Nennt ChatGPT den Betrieb, wenn Kunden nach einer Empfehlung fragen?", "Immer mehr Kunden fragen zuerst eine künstliche Intelligenz statt Google.", "k1"],
      ["Genannt, wenn Kunden ChatGPT ihr Problem beschreiben", "Named when customers describe their problem to ChatGPT", "Nennt ChatGPT den Betrieb, wenn Kunden ihr Problem beschreiben?", "Hier entstehen die Aufträge, bevor der Kunde einen Namen kennt.", "k2"]
    ] },
  { id: "anthropic", gewicht: 6.492, de: "Claude", en: "Claude",
    pruefungen: [
      ["Claude darf die Website lesen", "Claude may read the website", "Erlaubt die robots.txt den Abrufern Claude-SearchBot und Claude-User das Lesen der Website?", "Gesperrte Abrufer lassen den Betrieb in Antworten von Claude aus.", "a17"],
      ["Die Firewall lässt Claude durch", "The firewall lets Claude through", "Kommt Claude-SearchBot an der Firewall der Website vorbei?", "Eine blockierende Firewall sperrt Claude aus, obwohl die robots.txt es erlaubt.", "a18"],
      ["Genannt, wenn Kunden Claude nach einem Betrieb fragen", "Named when customers ask Claude about a business", "Nennt Claude den Betrieb, wenn Kunden nach einer Empfehlung fragen?", "Wer in Claude nicht genannt wird, fehlt bei allen Kunden, die dort suchen.", "k6"],
      ["Genannt, wenn Kunden Claude ihr Problem beschreiben", "Named when customers describe their problem to Claude", "Nennt Claude den Betrieb, wenn Kunden ihr Problem beschreiben?", "Hier entstehen Aufträge, bevor der Kunde überhaupt einen Namen kennt.", "k7"]
    ] }
];

export function karten(sprache) {
  const liste = [];
  bereiche.forEach((b, bi) => {
    b.pruefungen.forEach((p) => {
      liste.push({ bereich: bi, kennung: p[4], name: sprache === "en" ? p[1] : p[0], frage: p[2], wirkung: p[3] });
    });
  });
  return liste;
}

export function gewichtText(gewicht, sprache) {
  return gewicht.toLocaleString(sprache === "en" ? "en-GB" : "de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

export const texte = {
  de: { pruefungen: "Prüfungen", gewicht: "Gewicht" },
  en: { pruefungen: "checks", gewicht: "weight" }
};
