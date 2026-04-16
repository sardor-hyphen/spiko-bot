# Fix NameError in handlers.py - Deploy Issue
**Approved Plan Implementation**

## Steps to Complete:
1. [ ] Move `def setup_handlers(application: Application):` to after imports in bot/handlers.py
2. [ ] Replace ALL direct handler references in add_handler calls with lambda wrappers (e.g., `lambda update, context: tasks_handler(update, context)`)
3. [ ] Apply edit to bot/handlers.py using edit_file tool
4. [ ] Test locally: `cd bot && python main.py` - verify no NameError
5. [ ] Commit/push changes for Render redeploy
6. [ ] Verify Render logs: successful uvicorn startup
7. [ ] Mark complete and attempt_completion

**Status:** Ready for edit implementation
