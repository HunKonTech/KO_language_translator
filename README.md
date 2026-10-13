# Translation Script Documentation

## English

### Description
This Python script automates the translation of `.resx` resource files for a project. It takes the base resource file (`AppRes.resx`), detects other language-specific `.resx` files in the same directory, and translates missing or outdated entries using Google Translate. It provides a menu-driven interface for user interaction or can be run with command-line arguments. Users can specify languages to exclude from translation.

### How it Works
1. The script checks for `AppRes.resx` in the given directory.
2. It iterates over all `.resx` files, excluding specified languages.
3. It loads existing translations if available; otherwise, it creates a new `.resx` file.
4. It translates missing or outdated entries using Google Translate.
5. The translated text is inserted into the respective `.resx` files.
6. The script allows users to run it in interactive mode or via command-line arguments.

### Compilation & Execution

#### Linux
1. Ensure Python 3 is installed:
   ```sh
   sudo apt install python3
   ```
2. Install dependencies:
   ```sh
   pip install deep-translator
   ```
3. Run the script:
   ```sh
   python3 main.py --new-only --resx-directory /path/to/resx --exclude-languages "fr,de"
   ```
4. Create an executable:
   ```sh
   pyinstaller --onefile --console --hidden-import=deep_translator.main main.py
   ```

#### Windows
1. Ensure Python 3 is installed (download from [Python website](https://www.python.org/downloads/)).
2. Install dependencies:
   ```cmd
   pip install deep-translator
   ```
3. Run the script:
   ```cmd
   python main.py --new-only --resx-directory C:\path\to\resx --exclude-languages "fr,de"
   ```
4. Create an executable:
   ```cmd
   pyinstaller --onefile --console --hidden-import=deep_translator.main main.py
   ```

#### macOS
1. Ensure Python 3 is installed. You can use the official installer from [Python website](https://www.python.org/downloads/) or Homebrew:
   ```sh
   brew install python
   ```
2. Install dependencies:
   ```sh
   pip3 install deep-translator
   ```
3. Run the script:
   ```sh
   python3 main.py --new-only --resx-directory /path/to/resx --exclude-languages "fr,de"
   ```
4. Create an executable:
   ```sh
   pyinstaller --onefile --console --hidden-import=deep_translator.main main.py
   ```

### Dependencies
- `deep-translator` (install using `pip install deep-translator`)
- Optional: `argostranslate` for the offline fallback (`pip install argostranslate`). It pulls
  in PyTorch (about 1 GB installed) and is not bundled into the PyInstaller executable.

### Command-Line Arguments
- `--resx-directory <directory>`: Specifies the path to the `.resx` files directory.
- `--exclude-languages <languages>`: Comma-separated list of languages to exclude (e.g., `fr,de`).
- `--force`: Translates all entries regardless of existing values.
  - **Example:**
    ```sh
    python main.py --force --resx-directory /path/to/resx --exclude-languages "es,it"
    ```
- `--new-only`: Translates only missing or new entries.
  - **Example:**
    ```sh
    python main.py --new-only --resx-directory /path/to/resx --exclude-languages "fr,de"
    ```
- `--workers <n>`: Number of parallel translation workers (default: `8`). Translation
  requests run concurrently across all languages, while each output file is still built
  and written once on a single thread, so the generated files never get mixed up. Lower
  this (e.g. `4`) if Google Translate starts rate-limiting.
  - **Example:**
    ```sh
    python main.py --new-only --resx-directory /path/to/resx --workers 4
    ```
- `--batch-chars <n>`: Characters sent to Google Translate in one request (default:
  `3000`, at most `4900`). Many texts of the same language travel together, each after a
  numbered `XSEG<n>X` marker line, and the reply is split at the markers, so a language
  costs a handful of requests instead of one per text; sending one request per text gets
  the IP blocked by Google. A batch whose markers do not come back intact is halved and
  sent again, down to one text per request.
- `--microsoft-key <key>`: Azure AI Translator key; enables Microsoft as the first fallback
  (free F0 tier: 2M characters/month).
- `--microsoft-region <region>`: Region of the Azure Translator resource (e.g. `westeurope`).
  Needed for regional and multi-service resources, not for global ones.
- `--deepl-key <key>`: DeepL API key; enables DeepL as the next fallback. Free keys (ending
  in `:fx`) use the Free API automatically (500k characters/month).
- `--no-argos`: Disables the offline Argos Translate fallback, which is otherwise used
  whenever `argostranslate` is installed.
- `--resx-source-lang <lang>`: Language of `AppRes.resx` (default: `en`). Only Argos needs
  it; the online services detect the source language. For i18n files `--i18n-source-lang`
  is used.

#### Translation fallback
Translations go through the services in this order, each one only if configured:
**Google -> Microsoft -> DeepL -> Argos (offline)**. A service that is rate limited, out of
quota, rejects its key or cannot be reached is skipped for 5 minutes, and the batch goes to
the next one. A service that fails on a single text (for example an unsupported language)
only passes that text on. If every service is skipped, all are tried again rather than
failing at once. Argos downloads its language models (about 100 MB per pair, pivoting
through English when there is no direct model) the first time a pair is needed. At the end
of a run, a summary shows how many requests each service handled and why any was skipped.
Azure also limits characters per sliding minute (about 33,300 on the free F0 tier), so
Microsoft requests are paced to 27,000 characters/minute, and an HTTP 429 is retried up to
6 times, waiting for `Retry-After` or 10-60 seconds.
- **Example:**
  ```sh
  python main.py --new-only --resx-directory /path/to/resx --microsoft-key <key> --microsoft-region westeurope --deepl-key <key>
  ```

> Keys passed on the command line end up in the shell history; in scripts, read them from
> a secret store or environment variable and pass them as arguments, e.g. `--deepl-key "$DEEPL_KEY"`.

### TypeScript i18n support

Besides `.resx` files, the script can translate a **TypeScript i18n file** that
keeps every language in one inline dictionary object:

```ts
export type Lang = "en" | "hu" | "zh" | "ru";

const DICTS: Record<Lang, Dict> = {
  en: {
    "toolbar.refresh": "Refresh",
    "menu.createBranch": "Create branch {name}",
  },
  hu: {
    "toolbar.refresh": "Frissítés",
  },
  zh: { /* ... */ },
  ru: { /* ... */ },
};
```

#### How it works

1. The script locates the `const DICTS = { ... }` object literal in the file
   (the declaration may also be `let` / `var` and may carry a type annotation
   such as `: Record<Lang, Dict>`).
2. It splits the object into one dictionary per language, keeping key order.
3. The **source language** (`en` by default, configurable with
   `--i18n-source-lang`) is the reference. Every key that exists in the source
   is checked in the other languages.
   - `--new-only`: only keys that are missing (or empty) in a target language
     are translated.
   - `--force`: every key of every non-source language is re-translated.
4. `{placeholder}` interpolation tokens (used by the `t("key", { name })`
   helper) are **kept verbatim** — they are shielded before translation and
   restored afterwards, even if the translator changes their casing/spacing.
5. Language keys are mapped to Google Translate codes automatically
   (`zh` → `zh-CN`, `zh-tw` → `zh-TW`, `he` → `iw`, …).
6. Only the `DICTS` object literal is rewritten (using the source key order for
   every language). Imports, the `t()` function, types and everything else in
   the file stay untouched.
7. Progress is shown live as a percentage bar with the **last 4 translated
   lines** below it:
   ```
   [###############---------------]  52.3%  (130/248)
     (zh) settings.theme: Theme --> 主题
     (ru) toolbar.push: ⇧ Push --> ⇧ Толкать
   ```

#### Command-line arguments

- `--i18n-file <path>`: Path to the `.ts` file that contains the `DICTS` object.
- `--i18n-source-lang <lang>`: Source language key inside `DICTS` (default `en`).
- `--workers <n>`: Parallel translation workers (default `8`). All missing keys across
  every language are translated concurrently; the `DICTS` object is then rebuilt and
  written once, so the file stays consistent.
- `--batch-chars <n>`: Characters per request, see above (default `3000`).
- Use together with `--new-only` **or** `--force`, and optionally
  `--exclude-languages` (comma-separated language keys to skip, e.g. languages
  you maintain by hand).

#### Examples

Fill in only the missing keys for every language except `ru`:

```sh
python3 main.py --new-only \
  --i18n-file /path/to/project/packages/graph-webview/src/i18n.ts \
  --exclude-languages "ru"
```

Re-translate everything, but keep the hand-written `en` and `hu` dictionaries:

```sh
python3 main.py --force \
  --i18n-file /path/to/project/packages/graph-webview/src/i18n.ts \
  --exclude-languages "hu"
```

You can also use the interactive menu (options **3** and **4** cover the
TypeScript i18n file).

> On macOS use `python3` (and install the dependency with
> `pip3 install deep-translator --break-system-packages` if needed).

---

## Magyar

### Leírás
Ez a Python szkript automatikusan lefordítja a `.resx` erőforrásfájlokat egy projekt számára. Az alap `AppRes.resx` fájl alapján létrehozza vagy frissíti a többi nyelvi `.resx` fájlt a könyvtárban, és a hiányzó vagy elavult bejegyzéseket a Google Fordító segítségével lefordítja. Használható interaktív menüből vagy parancssori argumentumokkal. A felhasználók megadhatják a kizárni kívánt nyelveket.

### Működés
1. A szkript ellenőrzi az `AppRes.resx` fájl létezését.
2. Bejárja az összes `.resx` fájlt, kivéve a megadott nyelveket.
3. Betölti a meglévő fordításokat, vagy új `.resx` fájlt hoz létre.
4. A hiányzó vagy elavult bejegyzéseket a Google Fordítóval lefordítja.
5. A lefordított szövegeket beilleszti a megfelelő `.resx` fájlokba.
6. A szkript interaktív menüből vagy parancssorból is futtatható.

### Fordítás és futtatás

#### Linux
1. Telepítsd a Python 3-at:
   ```sh
   sudo apt install python3
   ```
2. Telepítsd a függőségeket:
   ```sh
   pip install deep-translator
   ```
3. Futtasd a szkriptet:
   ```sh
   python3 main.py --new-only --resx-directory /path/to/resx --exclude-languages "fr,de"
   ```
4. Hozz létre egy futtatható fájlt:
   ```sh
   pyinstaller --onefile --console --hidden-import=deep_translator.main main.py
   ```

#### Windows
1. Telepítsd a Python 3-at ([Python letöltés](https://www.python.org/downloads/)).
2. Telepítsd a függőségeket:
   ```cmd
   pip install deep-translator
   ```
3. Futtasd a szkriptet:
   ```cmd
   python main.py --new-only --resx-directory C:\path\to\resx --exclude-languages "fr,de"
   ```
4. Hozz létre egy futtatható fájlt:
   ```cmd
   pyinstaller --onefile --console --hidden-import=deep_translator.main main.py
   ```

#### macOS
1. Telepítsd a Python 3-at. Használhatod a hivatalos telepítőt a [Python letöltés](https://www.python.org/downloads/) oldalról, vagy a Homebrew-t:
   ```sh
   brew install python
   ```
2. Telepítsd a függőségeket:
   ```sh
   pip3 install deep-translator
   ```
3. Futtasd a szkriptet:
   ```sh
   python3 main.py --new-only --resx-directory /path/to/resx --exclude-languages "fr,de"
   ```
4. Hozz létre egy futtatható fájlt:
   ```sh
   pyinstaller --onefile --console --hidden-import=deep_translator.main main.py
   ```

### Függőségek
- `deep-translator` (telepíthető `pip install deep-translator` paranccsal)
- Opcionális: `argostranslate` az offline fallbackhez (`pip install argostranslate`). Behúzza
  a PyTorch-ot (telepítve kb. 1 GB), és a PyInstaller-es futtatható fájlba nem kerül bele.

### Parancssori argumentumok
- `--resx-directory <könyvtár>`: Az `.resx` fájlokat tartalmazó könyvtár elérési útja.
- `--exclude-languages <nyelvek>`: Kizárt nyelvek vesszővel elválasztott listája (pl. `fr,de`).
- `--force`: Az összes bejegyzést lefordítja, még ha már léteznek is.
  - **Példa:**
    ```sh
    python main.py --force --resx-directory /path/to/resx --exclude-languages "es,it"
    ```
- `--new-only`: Csak az új vagy hiányzó bejegyzéseket fordítja le.
  - **Példa:**
    ```sh
    python main.py --new-only --resx-directory /path/to/resx --exclude-languages "fr,de"
    ```
- `--workers <n>`: A párhuzamos fordító szálak száma (alapértelmezés: `8`). A fordítási
  kérések az összes nyelvre egyszerre futnak, de minden kimeneti fájl továbbra is egyetlen
  szálon, egyszer épül fel és íródik ki, így a generált fájlok soha nem keverednek össze.
  Csökkentsd (pl. `4`), ha a Google Fordító rate-limitelni kezd.
  - **Példa:**
    ```sh
    python main.py --new-only --resx-directory /path/to/resx --workers 4
    ```
- `--batch-chars <n>`: Egy kérésben a Google Fordítónak küldött karakterek száma
  (alapértelmezés: `3000`, legfeljebb `4900`). Egy nyelv több szövege együtt megy el,
  mindegyik egy számozott `XSEG<n>X` jelölősor után, és a válasz a jelölők mentén válik
  szét, így egy nyelv néhány kérésbe kerül szövegenkénti egy helyett; a szövegenkénti
  kérésekért a Google letiltja az IP-címet. Ha egy köteg jelölői nem érkeznek vissza
  épen, a köteg megfeleződik és újra elmegy, legrosszabb esetben szövegenként.
- `--microsoft-key <kulcs>`: Azure AI Translator kulcs; bekapcsolja a Microsoftot első
  fallbackként (ingyenes F0 csomag: havi 2M karakter).
- `--microsoft-region <régió>`: Az Azure Translator erőforrás régiója (pl. `westeurope`).
  Regionális és multi-service erőforrásnál kell, globálisnál nem.
- `--deepl-key <kulcs>`: DeepL API kulcs; bekapcsolja a DeepL-t következő fallbackként. Az
  ingyenes (`:fx` végű) kulcsok automatikusan a Free API-t használják (havi 500k karakter).
- `--no-argos`: Kikapcsolja az offline Argos Translate fallbacket, amely egyébként mindig
  működik, ha az `argostranslate` telepítve van.
- `--resx-source-lang <nyelv>`: Az `AppRes.resx` nyelve (alapértelmezés: `en`). Csak az
  Argosnak kell; az online szolgáltatók felismerik a forrásnyelvet. i18n fájloknál az
  `--i18n-source-lang` érték számít.

#### Fordítási fallback
A fordítás ebben a sorrendben megy végig a szolgáltatókon, mindegyiken csak ha be van
állítva: **Google -> Microsoft -> DeepL -> Argos (offline)**. Ha egy szolgáltató
rate-limitel, elfogyott a kerete, elutasítja a kulcsot vagy nem érhető el, 5 percre
kimarad, és a köteg a következőhöz kerül. Ha egy szolgáltató csak egy szövegnél hibázik
(pl. nem támogatott nyelv), csak azt a szöveget adja tovább. Ha minden szolgáltató
kimaradna, újra mindet megpróbálja, ahelyett hogy azonnal hibát adna. Az Argos egy
nyelvpár első használatakor letölti a modelleket (páronként kb. 100 MB; ha nincs közvetlen
modell, angolon keresztül fordít). A futás végén összesítő mutatja, melyik szolgáltató hány
kérést kapott, és miért maradt ki valamelyik.
Az Azure percenkénti karakterszámot is korlátoz (az ingyenes F0-n kb. 33 300 karakter egy
csúszó percben), ezért a Microsoft-kérések percenként legfeljebb 27 000 karaktert küldenek,
HTTP 429 esetén pedig legfeljebb 6-szor újrapróbálnak, a `Retry-After` szerint vagy 10-60
másodpercet várva.
- **Példa:**
  ```sh
  python main.py --new-only --resx-directory /path/to/resx --microsoft-key <kulcs> --microsoft-region westeurope --deepl-key <kulcs>
  ```

> A parancssorban átadott kulcsok bekerülnek a shell előzményekbe; szkriptekben titoktárból
> vagy környezeti változóból olvasd be és add át argumentumként, pl. `--deepl-key "$DEEPL_KEY"`.

### TypeScript i18n támogatás

A `.resx` fájlok mellett a szkript egy **TypeScript i18n fájlt** is le tud
fordítani, amely az összes nyelvet egyetlen soron belüli szótárobjektumban
tartja:

```ts
export type Lang = "en" | "hu" | "zh" | "ru";

const DICTS: Record<Lang, Dict> = {
  en: {
    "toolbar.refresh": "Refresh",
    "menu.createBranch": "Create branch {name}",
  },
  hu: {
    "toolbar.refresh": "Frissítés",
  },
  zh: { /* ... */ },
  ru: { /* ... */ },
};
```

#### Működés

1. A szkript megkeresi a `const DICTS = { ... }` objektumliterált a fájlban
   (lehet `let` / `var` is, és lehet rajta típusannotáció, pl.
   `: Record<Lang, Dict>`).
2. Az objektumot nyelvenként külön szótárra bontja, a kulcssorrendet megtartva.
3. A **forrásnyelv** (alapból `en`, a `--i18n-source-lang` kapcsolóval
   állítható) a referencia. A forrásban lévő minden kulcsot ellenőriz a többi
   nyelvben.
   - `--new-only`: csak a hiányzó (vagy üres) kulcsokat fordítja le az adott
     nyelvben.
   - `--force`: minden nem-forrásnyelv minden kulcsát újrafordítja.
4. A `{placeholder}` tokenek (amiket a `t("kulcs", { name })` hívás használ)
   **érintetlenül maradnak** – fordítás előtt védetté teszi, utána visszaállítja
   őket, még akkor is, ha a fordító megváltoztatja a kis-/nagybetűt vagy a
   szóközöket.
5. A nyelvi kulcsokat automatikusan Google Translate kódokra képezi le
   (`zh` → `zh-CN`, `zh-tw` → `zh-TW`, `he` → `iw`, …).
6. Csak a `DICTS` objektumliterál íródik újra (minden nyelvnél a forrás
   kulcssorrendjével). Az importok, a `t()` függvény, a típusok és minden más a
   fájlban változatlan marad.
7. A folyamat élőben látszik: százalékos csík, alatta a **4 legutóbbi lefordított
   sor**:
   ```
   [###############---------------]  52.3%  (130/248)
     (zh) settings.theme: Theme --> 主题
     (ru) toolbar.push: ⇧ Push --> ⇧ Толкать
   ```

#### Parancssori argumentumok

- `--i18n-file <útvonal>`: A `DICTS` objektumot tartalmazó `.ts` fájl elérési útja.
- `--i18n-source-lang <nyelv>`: A forrásnyelv kulcsa a `DICTS`-ben (alapértelmezés: `en`).
- `--workers <n>`: Párhuzamos fordító szálak száma (alapértelmezés: `8`). Az összes nyelv
  összes hiányzó kulcsa egyszerre fordul le; a `DICTS` objektum ezután egyszer épül újra és
  íródik ki, így a fájl konzisztens marad.
- `--batch-chars <n>`: Karakterek kérésenként, lásd fent (alapértelmezés: `3000`).
- A `--new-only` **vagy** `--force` kapcsolóval együtt használandó, opcionálisan
  a `--exclude-languages` kapcsolóval (vesszővel elválasztott nyelvi kulcsok,
  amiket ki kell hagyni – pl. amiket kézzel karbantartasz).

#### Példák

Csak a hiányzó kulcsok pótlása minden nyelvre, kivéve `ru`:

```sh
python3 main.py --new-only \
  --i18n-file /path/to/project/packages/graph-webview/src/i18n.ts \
  --exclude-languages "ru"
```

Minden újrafordítása, de a kézzel írt `en` és `hu` szótár megtartása:

```sh
python3 main.py --force \
  --i18n-file /path/to/project/packages/graph-webview/src/i18n.ts \
  --exclude-languages "hu"
```

Az interaktív menü is használható (a **3.** és **4.** opció a TypeScript i18n
fájlt kezeli).

> macOS-en `python3` a parancs (és ha kell, a függőség:
> `pip3 install deep-translator --break-system-packages`).
