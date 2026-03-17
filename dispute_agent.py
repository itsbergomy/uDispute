from browser_use import Agent, Controller, ActionResult
from browser_use.browser import BrowserSession
from browser_use.llm import ChatOpenAI
from dotenv import load_dotenv
import asyncio, os, json

load_dotenv()

# -------------------------
# Load SOP JSON
# -------------------------
with open("cfpb_complaint_flow.json", "r") as f:
    SOP_DATA = json.load(f)["steps"]

def get_step_data(step_name):
    for step in SOP_DATA["steps"]:
        if step["name"] == step_name:
            return step
    return None    

# -------------------------
# Sensitive Data
# -------------------------
sensitive_data = {
    'https://portal.consumerfinance.gov': {
        'x_cfpb_email': os.getenv("CFPB_EMAIL"),
        'x_cfpb_password': os.getenv("CFPB_PASSWORD"),
    }
}

# -------------------------
# Initialize Controller & Browser
# -------------------------
controller = Controller()

browser_session = BrowserSession(
    user_data_dir="playwright_user_data",
    storage_state="cfpb-auth.json",
    headless=False,
    allowed_domains=["https://portal.consumerfinance.gov", "https://www.consumerfinance.gov"]
)

DEFAULT_NARRATIVE = (
    "The debt collector reported inaccurate information regarding a debt I do not owe. "
    "I contacted the company multiple times, but they failed to provide proper validation of the debt."
)

DEFAULT_RESOLUTION = (
    "I request removal of the inaccurate account from my credit report and written confirmation "
    "that no further collection activity will occur."
)



# -------------------------
# Generic Validation Based on JSON
# -------------------------
async def validate_step(page, validation_rules):
    # URL check
    if "url_contains" in validation_rules:
        if validation_rules["url_contains"] not in page.url:
            return False

    # Element presence check
    if "element_presence" in validation_rules:
        for selector in validation_rules["element_presence"]:
            if not await page.query_selector(selector):
                return False

    # Selected labels check
    if "selected_labels" in validation_rules:
        for label in validation_rules["selected_labels"]:
            if not await page.query_selector(f"label:has-text('{label}').selected"):
                return False

    # Textareas non-empty check
    if validation_rules.get("textarea_nonempty"):
        filled = await page.locator("textarea").evaluate_all("elements => elements.every(e => e.value.trim() !== '')")
        if not filled:
            return False

    # Manual confirmation (pause and wait)
    if validation_rules.get("manual_confirmation"):
        input(">>> Confirm manual input complete, press ENTER to continue...")
        return True

    return True

async def handle_failure(context, instruction):
    page = await context.get_current_page()

    if "reload" in instruction.lower():
        await page.goto("https://portal.consumerfinance.gov/consumer/s/")
    elif "retry" in instruction.lower():
        await page.go_back()
    elif "pause" in instruction.lower():
        input(">>> Manual intervention required. Press ENTER to retry...")

# -------------------------
# Guarded Action Execution
# -------------------------
async def execute_guarded_action(step_name, step_fn, context, retries=3):
    page = await context.get_current_page()
    step_data = get_step_data(step_name)

    for attempt in range(retries):
        await step_fn(context)

        # Validate step dynamically
        validation_passed = await validate_step(page, step_data["validation"])
        if validation_passed:
            print(f"[PASS] {step_name} passed validation (attempt {attempt+1})")
            return True
        else:
            print(f"[FAIL] {step_name} failed validation (attempt {attempt+1})")
            await handle_failure(context, step_data["on_failure"])

    # Hard fail: restart from first step
    print(f"[HARD FAIL] Restarting process after {step_name} validation failures.")
    await navigate_and_start_complaint(context)
    return False

# -------------------------
# Actions (Refactored to use Guarded Execution)
# -------------------------

@controller.action("navigate_and_start_complaint")
async def navigate_and_start_complaint(context) -> ActionResult:
    page = await context.get_current_page()

    # Go directly to login page
    await page.goto("https://portal.consumerfinance.gov/consumer/s/login/")
    await page.wait_for_load_state("networkidle")

    print(">>> Please log in manually on the browser. Press ENTER once login is complete.")
    input()

    # Validation: confirm dashboard loaded
    if "portal.consumerfinance.gov/consumer/s/" not in page.url:
        raise Exception("Manual login failed: Not on CFPB portal dashboard.")

    return ActionResult(extracted_content="Manually logged in and navigated to portal dashboard")





@controller.action("select_debt")
async def guarded_select_debt(context) -> ActionResult:
    async def action_fn(context):
        page = await context.get_current_page()
        await page.click("label:has-text('Debt collection')")
        await page.click("label:has-text('I do not know')")
        await page.click("button:has-text('Next')")
    await execute_guarded_action(context, "select_debt", action_fn)
    return ActionResult(extracted_content="Debt collection validated")

@controller.action("select_problem_type_action")
async def guarded_select_problem_type(context) -> ActionResult:
    async def action_fn(context):
        page = await context.get_current_page()
        await page.click("label:has-text('Took or threatened to take negative or legal action')")
        await page.click("label:has-text('Threatened or suggested your credit would be damaged')")
        await page.click("button:has-text('Next')")
    await execute_guarded_action(context, "select_problem_type_action", action_fn)
    return ActionResult(extracted_content="Problem type validated")

@controller.action("fix_problem_questions_action")
async def guarded_fix_problem_questions(context) -> ActionResult:
    async def action_fn(context):
        page = await context.get_current_page()
        await page.click("label:has-text('Yes')")
        await page.click("button:has-text('Next')")
        await page.click("label:has-text('Yes')")
        await page.click("button:has-text('Next')")
        await page.click("label:has-text('No')")
        await page.click("button:has-text('Next')")
    await execute_guarded_action(context, "fix_problem_questions_action", action_fn)
    return ActionResult(extracted_content="Fix questions validated")

@controller.action("fill_happened_and_resolution")
async def fill_happened_and_resolution(context) -> ActionResult:
    page = await context.get_current_page()
    await page.locator("textarea").nth(0).fill(DEFAULT_NARRATIVE)
    await page.locator("textarea").nth(1).fill(DEFAULT_RESOLUTION)
    await page.click("button:has-text('Next')")
    return ActionResult(extracted_content="Narrative and resolution filled")

@controller.action("manual_company_account_action")
async def manual_company_account_action(context) -> ActionResult:
    print(">>> Please enter company name and account number manually in the browser.")
    input("Press ENTER once completed manually...")
    return ActionResult(extracted_content="Company and account entered manually")

@controller.action("who_submitted")
async def who_submitted(context) -> ActionResult:
    page = await context.get_current_page()
    await page.click("label:has-text('Myself')")
    await page.click("button:has-text('Next')")
    return ActionResult(extracted_content="Selected 'Myself'")

@controller.action("manual_personal_info_action")
async def manual_personal_info_action(context) -> ActionResult:
    print(">>> Please fill personal mailing info manually in the browser.")
    input("Press ENTER once completed manually...")
    return ActionResult(extracted_content="Personal info entered manually")

@controller.action("review_and_submit")
async def review_and_submit(context) -> ActionResult:
    page = await context.get_current_page()
    await page.wait_for_selector("text=Review your complaint", timeout=60_000)
    await page.click("button:has-text('Submit')")
    return ActionResult(extracted_content="Complaint submitted")

# -------------------------
# Agent Setup with New Prompt
# -------------------------
llm = ChatOpenAI(model="gpt-4o")

agent = Agent(
    task="File CFPB complaint for debt collection issue using strict SOP with JSON validation.",
    message_context="""
    The SOP is loaded from cfpb_complaint_flow.json. Follow JSON steps sequentially. 
    Validate each step using JSON validation rules before proceeding.
    No improvisation or extra clicks. On failure, apply JSON 'on_failure' instructions.
    """,
    llm=llm,
    browser_session=browser_session,
    controller=controller,
    sensitive_data=sensitive_data,
    use_vision=True,
    max_actions_per_step=3,
    max_failures=3,
    retry_delay=10,
    extend_system_message="""
    Persona: Deterministic automation assistant executing SOP exactly.
    Context: Filing CFPB complaint for inaccurate debt collection.
    Task: Complete steps 1-9 in JSON order, validating at each stage.
    Format: Log successes/failures, retry failures up to 3 times, halt if unrecoverable.
    Tone: Procedural and corrective. No improvisation.
    """
)

# -------------------------
# Run
# -------------------------
async def main():
    result = await agent.run(max_steps=50)
    print("Agent completed:", result)

if __name__ == "__main__":
    asyncio.run(main())
