from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
import requests
from bs4 import BeautifulSoup
import cv2
import easyocr
import re
import time
import random
import csv
import threading
import os
from concurrent.futures import ThreadPoolExecutor

app = FastAPI()
templates = Jinja2Templates(directory="templates")

PROGRAM_SELECT = "http://result.rgpv.ac.in/Result/ProgramSelect.aspx"
RESULT_PAGE    = "http://result.rgpv.ac.in/Result/BErslt.aspx"

DEPARTMENT = "1"
SEMESTER   = "6"

MAX_ATTEMPT_PER_ROLL = 5
THREADS = 4    # For FAST VALUE

# or ocr setup
reader = easyocr.Reader(['en'], gpu=False)


results = {}
lock = threading.Lock()

# CAPTCHA OCR

def solve_captcha(img_path):
    img = cv2.imread(img_path)
    if img is None:
        return "", 0.0

    img = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    out = reader.readtext(
        gray,
        detail=1,
        allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    )

    if not out:
        return "", 0.0

    text, conf = "", 0.0
    for _, t, c in out:
        text += t
        conf += c

    return re.sub(r'[^A-Za-z0-9]', '', text), conf / len(out)


# RESULT PARSER (100% CORRECT)

def parse_result(html):
    soup = BeautifulSoup(html, "html.parser")

    def clean(x):
        return x.text.replace("\n", "").strip() if x else "N/A"

    name = clean(soup.find("td", text=re.compile("Name")).find_next("td"))
    roll = clean(soup.find("td", text=re.compile("Roll")).find_next("td"))

    th = soup.find("th", text=re.compile("CGPA"))
    if not th:
        return None

    result_td = th.parent.find_next("td")
    sgpa_td = result_td.find_next("td")
    cgpa_td = sgpa_td.find_next("td")

    return {
        "Roll": roll,
        "Name": name,
        "SGPA": clean(sgpa_td),
        "CGPA": clean(cgpa_td),
        "Result": clean(result_td)
    }

# WORKER (PER ROLL)

def process_roll(i, roll_prefix, semester):
    roll_no = roll_prefix + str(i).zfill(3)
    captcha_file = f"captcha_{i}.png"

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    })

    try:
        r = session.get(PROGRAM_SELECT, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        did = f"radlstProgram_{DEPARTMENT}"
        val = soup.find("input", {"id": did})["value"]

        session.post(PROGRAM_SELECT, data={
            "__EVENTTARGET": did.replace("_", "$"),
            "__EVENTARGUMENT": "",
            "__LASTFOCUS": "",
            "__VIEWSTATE": soup.find("input", {"id": "__VIEWSTATE"})["value"],
            "__VIEWSTATEGENERATOR": soup.find("input", {"id": "__VIEWSTATEGENERATOR"})["value"],
            "__EVENTVALIDATION": soup.find("input", {"id": "__EVENTVALIDATION"})["value"],
            "radlstProgram": val
        })
    except:
        return

    for _ in range(MAX_ATTEMPT_PER_ROLL):
        try:
            r = session.get(RESULT_PAGE, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")

            imgs = soup.find_all("img")
            if len(imgs) < 2:
                continue

            cap = session.get("http://result.rgpv.ac.in/Result/" + imgs[1]["src"])
            with open(captcha_file, "wb") as f:
                f.write(cap.content)

            text, conf = solve_captcha(captcha_file)
            if conf < 0.9 or len(text) < 4:
                continue

            time.sleep(random.uniform(5.0, 7.0))

            post = session.post(RESULT_PAGE, data={
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "__LASTFOCUS": "",
                "__VIEWSTATE": soup.find("input", {"id": "__VIEWSTATE"})["value"],
                "__VIEWSTATEGENERATOR": soup.find("input", {"id": "__VIEWSTATEGENERATOR"})["value"],
                "__EVENTVALIDATION": soup.find("input", {"id": "__EVENTVALIDATION"})["value"],
                "ctl00$ContentPlaceHolder1$txtrollno": roll_no,
                "ctl00$ContentPlaceHolder1$drpSemester": semester,
                "ctl00$ContentPlaceHolder1$rbtnlstSType": "G",
                "ctl00$ContentPlaceHolder1$TextBox1": text,
                "ctl00$ContentPlaceHolder1$btnviewresult": "View Result"
            })

            if "resultheader" in post.text.lower():
                data = parse_result(post.text)
                if data:
                    with lock:
                        results[data["Roll"]] = data
                return
        except:
            continue
        finally:
            if os.path.exists(captcha_file):
                os.remove(captcha_file)

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/scrape", response_class=HTMLResponse)
async def scrape(request: Request, prefix: str = Form(...), semester: str = Form(...), start: int = Form(...), end: int = Form(...)):
    global results
    results = {}

    start_time = time.time()

    with ThreadPoolExecutor(max_workers=THREADS) as exe:
        exe.map(lambda i: process_roll(i, prefix, semester), range(start, end + 1))

    # Save to CSV
    with open("result.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["Roll", "Name", "SGPA", "CGPA", "Result"]
        )
        writer.writeheader()
        for r in sorted(results.keys()):
            writer.writerow(results[r])

    elapsed = round(time.time() - start_time, 2)
    total = len(results)

    # Sort results by roll
    sorted_results = sorted(results.values(), key=lambda x: x["Roll"])

    return templates.TemplateResponse("results.html", {"request": request, "results": sorted_results, "elapsed": elapsed, "total": total})

@app.get("/download")
async def download_csv():
    return FileResponse("result.csv", media_type="text/csv", filename="results.csv")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
