# Flashcards in Anki

Argus writes one `Flashcards.md` per course (`Courses/<code>/Flashcards.md`). Every card in it was checked by the verifier against your course materials. The [Obsidian_to_Anki](https://github.com/ObsidianToAnki/Obsidian_to_Anki) plugin sends those cards to Anki.

## One-time setup (on your laptop)
1. In Anki, install the **AnkiConnect** add-on (code `2055492159`) and restart Anki.
2. In Obsidian, install the community plugin **Obsidian_to_Anki** and enable it.
3. In the plugin's settings:
   - Turn on **Add File Link** if you want each card to link back to its course note (optional).
   - Leave the default note type **Basic** and the default `START`/`END` markers.
   - Under **Scan Directory**, enter `Courses` so the plugin only reads course folders.
4. With Anki open, click the Anki icon in Obsidian's ribbon (or run "Obsidian_to_Anki: Scan Vault").

## What Argus writes
```
TARGET DECK: Argus::CMSC351

START
Basic
Solve T(n) = 2T(n/2) + n.
Back: Theta(n log n), by case 2.
Tags: argus CMSC351 master-theorem
END
```
- **Decks:** one per course, nested under `Argus` (`Argus::CMSC351`).
- **Tags:** every card is tagged `argus`, the course code, and the concept, so you can filter or suspend by topic.
- **Anki ids:** after the first scan, the plugin adds a line like `<!--ID: 1566052191670-->` above each `END`. Argus keeps these lines whenever it rewrites the file, so editing a card in Argus updates the existing Anki note instead of creating a duplicate. Argus also ignores those lines when it checks whether you edited the file, so a scan never turns into a proposal.

## Editing cards
The cards sit inside an Argus region (`%% argus:begin cards %%`). If you edit a card there, Argus stops rewriting the file and proposes its next version on the Proposals page instead. To add cards of your own, write them **below** the region (after `%% argus:end cards %%`), using the same `START`/`END` format. Argus never touches that part of the file.

## If something goes wrong
- **Duplicate cards:** a card's front text changed and its `<!--ID-->` line was lost. Delete the duplicate in Anki; the next scan relinks the card.
- **Cards don't appear:** Anki must be running with AnkiConnect when you scan. Check that the folder is listed under **Scan Directory**.
