# Run, test, deploy

## 1. Run locally (Python 3.10+, nothing to install)

```bash
python server.py                       # listens on :8080 (set PORT to change)
curl localhost:8080/v1/healthz
```

## 2. Test

```bash
python generate_dataset.py --seed-dir dataset --out expanded   # 50 merchants / 200 customers / 100 triggers
python tests/harness_test.py           # full judge lifecycle: warmup, ticks, replies, replays, injection
python tests/run_judge_sim.py all      # the official judge_simulator scenarios (stub scorer, no API key needed)
python tests/dump_all.py               # print + validate the message for all 100 triggers
python make_submission.py              # writes submission.jsonl (30 test pairs)
```

To get real LLM scores from the official simulator, edit the CONFIGURATION block in `judge_simulator.py`:
set `LLM_PROVIDER`, `LLM_API_KEY` and `BOT_URL`, then run `python judge_simulator.py`.

## 3. Deploy (pick one — must be a single, always-on instance)

State lives in memory and the judge fails you after 3 missed health checks, so avoid hosts that sleep
or scale to zero.

### Option A — Render (easiest)
1. Push this folder to a GitHub repo.
2. Render dashboard → **New → Blueprint** → select the repo (it reads `render.yaml`).
3. Keep the **Starter** plan (always on). The free plan sleeps after inactivity.
4. Set `VERA_TEAM_NAME`, `VERA_TEAM_MEMBERS` and `VERA_CONTACT_EMAIL` under Environment.
5. Your URL is `https://vera-bot-xxxx.onrender.com`. Check it with `curl https://…/v1/healthz`.

### Option B — Railway
1. **New Project → Deploy from GitHub repo**. Railway detects the `Procfile`.
2. Under Settings → Networking, click **Generate Domain**.
3. Set the same environment variables.

### Option C — Fly.io
```bash
fly launch --no-deploy        # keep the fly.toml in this folder; change the app name
fly deploy
fly scale count 1
```

### Option D — quick tunnel (testing only)
```bash
python server.py & ngrok http 8080
```

## 4. Before submitting the URL
- `curl https://YOUR-URL/v1/healthz` returns `status: ok`.
- `curl https://YOUR-URL/v1/metadata` shows your team name and email.
- Run `python tests/harness_test.py https://YOUR-URL` and confirm `FAILURES: 0`.
- Don't redeploy during the test window. A restart wipes the in-memory contexts.
- Optional LLM: set `VERA_LLM_PROVIDER` (`anthropic` or `openai`) and `VERA_LLM_API_KEY`, plus `VERA_LLM_POLISH=1` to also rephrase outbound drafts. Put a spend cap on the key.
