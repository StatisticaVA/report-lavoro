#!/usr/bin/env python3
"""Scarica da ISTAT (IstatData, servizio SDMX) i dati annuali della disoccupazione e li salva in dati/.

Serve alla pagina «Lavoro» (sezione Disoccupazione): la pagina non può scaricare da ISTAT da sola (il servizio non
permette la lettura da un browser), quindi lo fa questo script, lanciato da GitHub Actions ogni settimana.
Solo libreria standard di Python. Uso a mano:  python3 aggiorna_dati.py

Scrive:
  dati/disoccupazione_tassi.csv         tasso di disoccupazione (Varese, Lombardia, Italia, 12 province, sesso, età)
  dati/disoccupazione_disoccupati.csv   disoccupati (migliaia)
  dati/aggiornamento.json               ultimo anno, data di ultimo aggiornamento ISTAT, data del controllo
"""
import csv, io, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

CARTELLA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dati")
SDMXWS = "https://esploradati.istat.it/SDMXWS/rest/"
ANNO_INIZIO = 2018
PROVINCE = ["ITC41", "ITC42", "ITC44", "ITC45", "ITC46", "ITC47", "ITC48", "ITC4A", "ITC4B", "ITC43", "ITC49", "IT108"]
AREE = "+".join(["IT", "ITC4"] + PROVINCE)
SESSI = "1+2+9"
FASCE = "+".join(["Y15-24", "Y25-34", "Y15-34", "Y35-49", "Y20-64", "Y15-64", "Y15-74", "Y50-74"])
TAVOLE = [
    {"file": "disoccupazione_tassi.csv", "flow": "IT1,151_914,1.0", "dataflow": "IT1/151_914/1.0",
     "chiave": f"A.{AREE}.UNEM_R.{SESSI}.{FASCE}.99.TOTAL.TOTAL"},
    {"file": "disoccupazione_disoccupati.csv", "flow": "IT1,151_929_DF_DCCV_DISOCCUPT1_7,1.0",
     "dataflow": "IT1/151_929_DF_DCCV_DISOCCUPT1_7/1.0",
     "chiave": f"A.{AREE}.UNEMP.{SESSI}.Y15-74.99.TOTAL.99.TOTAL"},
]
COLONNE = ["REF_AREA", "DATA_TYPE", "SEX", "AGE", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS"]
PAUSA = 13          # ISTAT: circa 5 richieste al minuto per indirizzo


def richiesta(url, accept, tentativi=4):
    ultimo = None
    for n in range(1, tentativi + 1):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"Accept": accept, "Accept-Language": "it, en;q=0.5",
                                                       "User-Agent": "Mozilla/5.0 (report-lavoro/1.0)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                dati = r.read()
                print(f"  HTTP {r.status}, {len(dati)} byte, {time.time() - t0:.1f} s", flush=True)
                return dati
        except urllib.error.HTTPError as e:
            ultimo = f"HTTP {e.code}"
            print(f"  tentativo {n}/{tentativi}: {ultimo} ({e.reason})", flush=True)
            if e.code in (400, 404):
                break
        except Exception as e:  # rete, timeout
            ultimo = f"{type(e).__name__}: {e}"
            print(f"  tentativo {n}/{tentativi}: {ultimo}", flush=True)
        if n < tentativi:
            time.sleep(30 * n)
    raise RuntimeError(f"ISTAT non risponde ({ultimo}): {url}")


def ultimo_aggiornamento(dataflow):
    xml = richiesta(f"{SDMXWS}dataflow/{dataflow}?references=none", "application/xml").decode("utf-8", "replace")
    i = xml.find('id="LAST_UPDATE"')
    if i < 0:
        return None
    a = xml.find("<common:AnnotationTitle>", i)
    b = xml.find("</common:AnnotationTitle>", a)
    return xml[a + 24:b] if a > 0 and b > a else None


def main():
    os.makedirs(CARTELLA, exist_ok=True)
    anno = datetime.now(timezone.utc).year
    esito = {"controllato": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tavole": {}}
    for t in TAVOLE:
        print(f"\n== {t['file']}", flush=True)
        url = f"{SDMXWS}data/{t['flow']}/{t['chiave']}?startPeriod={ANNO_INIZIO}&endPeriod={anno}"
        dati = richiesta(url, "application/vnd.sdmx.data+csv;version=1.0.0, text/csv")
        righe = list(csv.DictReader(io.StringIO(dati.decode("utf-8-sig"))))
        if not righe or any(c not in righe[0] for c in COLONNE[:6]):
            raise RuntimeError(f"{t['file']}: risposta ISTAT non nel formato atteso (colonne: {list(righe[0]) if righe else 'nessuna riga'})")
        righe = [r for r in righe if r["OBS_VALUE"] not in ("", None)]
        anni = sorted({int(r["TIME_PERIOD"]) for r in righe})
        aree = {r["REF_AREA"] for r in righe}
        print(f"  {len(righe)} valori, anni {anni[0]}-{anni[-1]}, {len(aree)} territori", flush=True)
        time.sleep(PAUSA)
        agg = ultimo_aggiornamento(t["dataflow"])
        print(f"  ultimo aggiornamento ISTAT: {agg}", flush=True)
        time.sleep(PAUSA)
        vecchio = os.path.join(CARTELLA, t["file"])
        with open(vecchio + ".nuovo", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(COLONNE)
            for r in sorted(righe, key=lambda r: (r["REF_AREA"], r["SEX"], r["AGE"], r["TIME_PERIOD"])):
                w.writerow([r.get(c, "") for c in COLONNE])
        os.replace(vecchio + ".nuovo", vecchio)
        esito["tavole"][t["file"]] = {"righe": len(righe), "primo_anno": anni[0], "ultimo_anno": anni[-1],
                                      "territori": len(aree), "ultimo_aggiornamento_istat": agg}
    with open(os.path.join(CARTELLA, "aggiornamento.json"), "w", encoding="utf-8") as f:
        json.dump(esito, f, ensure_ascii=False, indent=1)
    print("\nFatto.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)
