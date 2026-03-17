import os
import sys
import uuid
import json
import base64
import asyncio
from dotenv import load_dotenv
from openai import OpenAI
from agents import function_tool
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Load environment and initialize client
load_dotenv()
client = OpenAI()

# Constants
STORAGE_FILE = "cfpb-auth.json"
USER_DATA_DIR = "playwright_user_data"
_sessions: dict[str, any] = {}

# Helper to retrieve page by session
def get_page(session_id: str):
    page = _sessions.get(session_id)
    if not page:
        raise ValueError(f"Invalid session_id: {session_id}")
    return page

@function_tool
async def wait_for_cfpb_login(timeout_ms: int = 100_000) -> dict[str, str]:
    """Launch the CFPB complaint browser asynchronously and return {'session_id': ...}."""
    # start Playwright and persistent context once
    if "_playwright" not in globals():
        globals()["_playwright"] = await async_playwright().start()
    if "_browser_context" not in globals():
        globals()["_browser_context"] = await globals()["_playwright"].chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            slow_mo=1000
        )
    context = globals()["_browser_context"]
    page = await context.new_page()
    await page.goto("https://www.consumerfinance.gov/complaint/")
    # prompt user to log in if needed
    if not os.path.exists(STORAGE_FILE):
        print("🔐 Please log in manually in the browser window…")
        await context.storage_state(path=STORAGE_FILE)
    # wait for complaint form to load after login
    await page.wait_for_selector("text=What is this complaint about?", timeout=timeout_ms)
    session_id = str(uuid.uuid4())
    _sessions[session_id] = page
    return {"session_id": session_id}

@function_tool
async def click_continue(session_id: str) -> dict[str, str]:
    """Click the Next/Continue button on the current CFPB page."""
    page = get_page(session_id)
    for selector in ("button:has-text('Next')", "button:has-text('Continue')"):
        try:
            await page.wait_for_selector(selector, state="visible", timeout=10_000)
            await page.click(selector)
            return {"status": "clicked"}
        except PlaywrightTimeoutError:
            continue
    raise RuntimeError("❌ Could not find a Next/Continue button")

@function_tool
async def select_debt_collection(session_id: str) -> dict[str, str]:
    """Select 'Debt collection' → 'I do not know' → Next."""
    page = get_page(session_id)
    await page.wait_for_selector("text=What is this complaint about?", timeout=60_000)
    await page.click("label:has-text('Debt collection')")
    await page.wait_for_selector("text=What type of debt?", timeout=60_000)
    await page.click("label:has-text('I do not know')")
    await click_continue.on_invoke_tool(None, json.dumps({"session_id": session_id}))
    return {"status": "debt_collection_selected"}

@function_tool
async def select_problem(session_id: str) -> dict[str, str]:
    """Select the specific problem options then click Next."""
    page = get_page(session_id)
    await page.wait_for_selector("text=What type of problem are you having?", timeout=60_000)
    await page.click("label:has-text('Took or threatened to take negative or legal action')")
    await page.wait_for_selector("text=Which best describes your problem?", timeout=60_000)
    await page.click("label:has-text('Threatened or suggested your credit would be damaged')")
    await page.click("button:has-text('Next')")
    return {"status": "problem_selected"}

@function_tool
async def click_no_with_vision(session_id: str) -> dict:
    """Use GPT-4 Vision to find & click the 'No' radio."""
    page = get_page(session_id)

    # 1) Clip just the fieldset region around Q3
    q3 = page.locator(
        "fieldset",
        has=page.locator("legend", has_text="Did the company provide this information")
    )
    box = await q3.bounding_box()
    if not box:
        raise RuntimeError("Couldn't find Q3 fieldset on screen!")
    
    # 2) Screenshot & encode
    shot = await page.screenshot(clip=box)
    b64   = base64.b64encode(shot).decode()

    # 3) Send to Vision with the correct async call
    resp = await client.chat.completions.acreate(
        model="gpt-4o-mini",
        messages=[
          {
            "role": "user",
            "content": [
              { "type": "image_url", "image_url": { "url": f"data:image/png;base64,{b64}" } },
              { "type": "text",  "text": "Return JSON with the pixel coordinates of the CENTER of the “No” radio." }
            ]
          }
        ],
        temperature=0
    )

    raw = resp.choices[0].message.content
    print("🔍 Vision raw response:", raw)                  # <— inspect the JSON string
    coords = json.loads(raw)
    print("🔍 Parsed coordinates:", coords)                # <— see { "x": .., "y": .. }

    # 4) Click at the returned coords
    x = box["x"] + coords["x"]
    y = box["y"] + coords["y"]
    await page.mouse.click(x, y)

    # 5) Return for testability/logging
    return coords

@function_tool
async def fix_problem(session_id: str) -> dict[str, str]:
    """Answer the three Q's: Yes, Yes, No (scoped click inside Q3)."""
    page = get_page(session_id)

    # — Q1 —
    await page.wait_for_selector(
        "text=Have you already tried to fix this problem with the company?",
        timeout=60_000
    )
    await page.locator("label:has-text('Yes')").first.click()
    await click_continue.on_invoke_tool(None, json.dumps({"session_id": session_id, "scope": "fieldset:has-text('fix this problem')"}))

    # — Q2 —
    await page.wait_for_selector(
        "text=Did you request information from the company?",
        timeout=60_000
    )
    await page.locator("label:has-text('Yes')").nth(1).click()
    await click_continue.on_invoke_tool(None, json.dumps({"session_id": session_id, "scope": "fieldset:has-text('request information')"}))

          # — Q3 —
    # Wait for the question text
    await page.wait_for_selector(
        "text=Did the company provide this information",
        timeout=60_000
    )

    # Use the Vision‐powered helper to find & click the 'No' radio by sight
    await click_no_with_vision(page)

    # Then advance using your reliable click_continue tool
    await click_continue.on_invoke_tool(None, json.dumps({"session_id": session_id}))

    return {"status": "fix_problem_completed"}

@function_tool
async def fill_happened_and_resolution(session_id: str, narrative: str, resolution: str) -> dict[str, str]:
    """Fill 'Tell us what happened' & 'fair resolution', then Next."""
    page = get_page(session_id)
    await page.wait_for_selector("text=Tell us what happened", timeout=300_000)
    tx = page.locator("textarea")
    await tx.nth(0).fill(narrative)
    await tx.nth(1).fill(resolution)
    await page.click("button:has-text('Next')")
    return {"status": "happened_and_resolution_filled"}

@function_tool
async def fill_company_and_account(session_id: str, company: str, account: str) -> dict[str, str]:
    """Fill company name & account number, then Next."""
    page = get_page(session_id)
    await page.wait_for_selector("text=Collection company that contacted you about the debt", timeout=60_000)
    inputs = page.locator("input[type='text']")
    await inputs.nth(0).fill(company)
    await inputs.nth(1).fill(account)
    await page.click("label:has-text(\"No / I don't know\")")
    await page.click("button:has-text('Next')")
    return {"status": "company_and_account_filled"}

@function_tool
async def who_submitted(session_id: str) -> dict[str, str]:
    """Select 'Myself / I am submitting the complaint for myself', then Next."""
    page = get_page(session_id)
    await page.wait_for_selector("text=Who are you submitting this complaint for?", timeout=60_000)
    await page.click("label:has-text('Myself / I am submitting the complaint for myself')")
    await page.click("button:has-text('Next')")
    return {"status": "who_submitted"}

@function_tool
async def fill_personal_info(session_id: str, address1: str, city: str, state: str, zip_code: str, address2: str = "", language: str = "") -> dict[str, str]:
    """Fill mailing address, ZIP, optional address2/language, then Next."""
    page = get_page(session_id)
    await page.fill("input[name='addressLine1']", address1)
    if address2:
        await page.fill("input[name='addressLine2']", address2)
    await page.fill("input[name='city']", city)
    await page.select_option("select[name='state']", label=state)
    await page.fill("input[name='zip']", zip_code)
    if language:
        await page.select_option("select[name='preferredLanguage']", label=language)
    await page.click("button:has-text('Next')")
    return {"status": "personal_info_filled"}

@function_tool
async def review_your_complaint(session_id: str) -> dict[str, str]:
    """Submit the complaint on the final review page."""
    page = get_page(session_id)
    await page.wait_for_selector("text=Review your complaint", timeout=60_000)
    await page.click("button:has-text('Submit')")
    return {"status": "complaint_submitted"}

# Registry of tools
TOOLS = {
    tool.name: tool
    for tool in [
        wait_for_cfpb_login, click_continue, select_debt_collection, select_problem,
        fix_problem, fill_happened_and_resolution, fill_company_and_account,
        who_submitted, fill_personal_info, review_your_complaint
    ]
}

# Main entrypoints
async def agent_loop():
    messages = [
        {"role": "system", "content": "You are an RPA agent. Automate the CFPB complaint flow."},
        {"role": "user",   "content": "Run the full CFPB complaint process from login through submission."}
    ]
    while True:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            functions=[{"name": t.name, "description": t.description, "parameters": t.params_json_schema} for t in TOOLS.values()],
            function_call="auto"
        )
        msg = resp.choices[0].message
        if msg.function_call is not None:
            name = msg.function_call.name
            args_json = msg.function_call.arguments or "{}"
            tool_obj  = TOOLS[name]
            # invoke the async tool in its own loop
            result = await tool_obj.on_invoke_tool(None, args_json)
            messages.append({"role":"assistant","content":None,"function_call":{"name":name,"arguments":args_json}})
            messages.append({"role":"function","name":name,"content":json.dumps(result)})
            continue
        print("🧠 Agent:", msg.content)
        break

if __name__ == "__main__":
    if "--agent" in sys.argv:
        asyncio.run(agent_loop())
    else:
        # fallback to manual flow if needed
        print("Pass --agent to run the CFPB RPA agent.")


