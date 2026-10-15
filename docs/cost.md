Cost per agent per day, from the recorded journal.

The price table in the proxy is 2 dollars per million input tokens and 6 dollars per million output tokens for primary. The local model is 0 dollars. Its cost is compute on the machine that serves it, not a line on this bill.

Daily brief, one recorded run, primary:

204 input tokens times 2 dollars, plus 199 output tokens times 6 dollars, divided by 1,000,000.

408 plus 1,194 is 1,602. Divided by 1,000,000 is 0.001602 dollars. The journal row for record-daily-brief step 3 stores that figure.

Four tenants each run one brief a day. 4 times 0.001602 is 0.006408 dollars per day for the brief agent.

Document agent, one recorded run, primary:

84 input tokens times 2, plus 56 output tokens times 6, divided by 1,000,000.

168 plus 336 is 504. Divided by 1,000,000 is 0.000504 dollars. The journal row for record-doc-analyst step 4 stores that figure.

Four tenants each run one document agent a day. 4 times 0.000504 is 0.002016 dollars per day.

Both agent types together are 0.008424 dollars per day at this recording's token counts. A longer brief costs more. The local fallback adds no API charge.
