# Daily ML Practice App Specification

Version 1.5, approved by the learner in Q20 on 17 November 2026. Consolidates the learner's confirmed personal-trial decisions and the approved implementation defaults.

## Learner-confirmed scope

The learner clarified the following decisions during the design interview on 17 November 2026. These decisions govern the current personal trial and take precedence over earlier detailed proposals in this document.

- Practise and reinforce ML fundamentals first, alongside introductory deployment and higher-level MLOps understanding. Deeper ML engineering and LLM topics follow as the curriculum grows.
- Practice is self-paced and untimed, with a daily minimum of two distinct questions. The learner can continue with more questions whenever they have time. Unfinished answers remain editable across days. Credit each completed question to the day it is finished, preserving the actual counts on earlier days.
- Include a practice calendar showing zero completed questions as no practice, one as partial, and two or more as goal met, with the actual count visible. A submitted answer counts immediately, including pending or disputed grading. “I don't know” counts after viewing the explanation. Drafts and revisions add no extra count.
- Start with approximately 12 carefully reviewed questions and a complete answer–feedback–review loop, saved answers, working grading, review scheduling, simple history, and backup. Add questions over time. Richer analytics, reminders, and the larger question bank belong to later expansion.
- The learner approved the 12-question starter allocation in Q19 and delegated prompt, hint, marking-guide, explanation, and source authoring/review to implementation. Section 5 records the confirmed topics.
- Organise questions into topic sections within learning areas. The learner's ML examples are fundamentals, forests, ensembles, and deep learning, with LLM sections added later. The learner can browse and choose an available section, with due reviews available alongside sections.
- The daily pair contains one randomly selected due review when available and one varied question favouring less recently practised sections, from a different section when possible. Without due reviews, both questions use the varied selection. Extra questions continue the mix or come from a learner-selected section.
- Random selection uses the reviewed question bank. Revisit existing questions when appropriate and add reviewed content as the bank grows.
- Use brief general-purpose ML examples. The learner explicitly rejected WiFi and Bluetooth scenarios and workplace-specific crowd analytics as curriculum themes.
- Let the learner flag an assessment as incorrect and retain the answer, assessment, and objection together. While a dispute is open, calculate learning progress and review dates from other undisputed assessments. Let the learner request reassessment against the marking guide and references, inspect both explanations, and accept either assessment or leave the dispute open.
- When a dispute leaves no accepted assessment, show “Awaiting accepted assessment” and keep the concept in the practice rotation. Clear dates derived solely from disputed evidence and retain any learner-requested earlier review.
- Use four assessment levels. Independently correct answers on different days progress through review gaps of 1, 3, 7, 14, and 30 days. Substantial gaps, an incorrect answer, “I don't know,” or help before answering bring the concept back tomorrow. A mostly correct answer with an important omission returns in three days. Revisions after feedback are assisted practice, with one revision available per original answer.
- Lead with the grade, what was understood, important corrections, and a concise model answer. Offer expandable reasoning, a brief example, and supporting sources. A follow-up question is optional to attempt.
- Use the learner's ChatGPT plan usage controls. Show local request counts and connection status, preserve answers awaiting feedback when access is unavailable, and offer later retry.
- Closely reproduce the MagicPath design using a React interface and Python backend, as recorded in the React/Python design and ADR 0001.

The learner answered both overlapping interview rounds. The earlier review proposal returned every partial answer tomorrow. This specification follows the later, more specific approval that returns a mostly correct answer with an important omission in three days. Other gaps and pre-answer help return tomorrow. The learner confirmed this interpretation in Q20.

The curriculum roadmap in section 4 describes future growth. The personal trial uses approximately 12 reviewed questions, simple history, a practice calendar, and backup and restore. Full Progress analytics and reminders follow trial use. The author-proposed technical defaults below were included in the learner's Q20 design approval.

## 1 Purpose and learner

Build a personal learning app for macOS with a React interface and Python backend. The initial curriculum provides practice of ML fundamentals and introductory deployment and MLOps concepts. The learner writes an answer, receives LLM feedback, and revisits concepts over time.

The learner understands mathematics and data science and wants to practise explaining ML concepts before expanding into deeper ML engineering. Explanations should connect theory to model behaviour and general deployment decisions. Use brief, general-purpose examples.

Success means the learner can recall a concept after a delay, explain how it works, recognise its assumptions and limitations, and apply it to an unfamiliar scenario. Track evidence from attempts and reviews. Describe progress in terms of practice and demonstrated understanding.

## 2 Decisions and delivery boundary

The user selected a Python app running on their Mac and ChatGPT Pro plan usage through Sign in with ChatGPT. The specification author proposed the remaining technical defaults below, and the learner approved the complete design in Q20. Account eligibility and integration behaviour require the implementation evidence specified below.

| Decision | Version 1 default | Reason |
| --- | --- | --- |
| Interface | React interface with a Python backend in the Mac's browser | The learner selected close reproduction of the MagicPath design |
| Storage | SQLite using Python's sqlite3 module | Durable single-user storage with transactions |
| Model connection | Sign in with ChatGPT using the learner's Pro allowance, with the official OpenAI Python SDK and Responses API | Prove account eligibility and the current preview contract in the first implementation milestone |
| Practice pace | Self-paced and untimed, minimum two questions daily, with more available on demand | Practice happens in the learner's own time |
| Content | Approximately 12 reviewed questions for the personal trial, expandable by topic section | Trial the complete learning loop before expanding content |
| Review policy | Four assessment levels and the confirmed 1/3/7/14/30-day progression | Explainable scheduling that can be tested |
| Timezone | Europe/London, configurable | Matches the learner's current location |
| Usage controls | ChatGPT plan controls, with local request counts displayed | Supports additional practice within the plan's available allowance |

Version 1 is a complete single-user local trial. Its deliverables are the app, reviewed starter content, automated tests, backup and restore, a macOS setup guide, and recovery instructions. Planning this application authorises preparation of this specification. Implementation follows approval of the design and the applicable Jira workflow.

### Approaches considered

A local browser app was selected by the user because it fits their Python skills and Mac workflow. A hosted website would add phone access and remotely scheduled reminders, with hosting and account administration. Practice directly inside ChatGPT would provide a quick conversational experience, with persistent scheduling and progress dependent on a supported storage integration.

### OpenAI connection

Use OpenAI's documented Sign in with ChatGPT integration for locally hosted apps. The learner selects “Continue with ChatGPT” in the app, signs in through the system browser, and grants permission to use their ChatGPT plan. Eligible grading requests consume the existing Codex / ChatGPT work allowance included in their Pro subscription. This is the required version 1 connection. The release must work through plan-authorized sign-in. Any later separately billed connection requires the learner's explicit approval. [Local-app integration](https://developers.openai.com/siwc/token-sharing-open-source)

The integration is currently a preview. Make a successful real-account sign-in, account-specific model lookup, and one completed schema-validated grading request the first implementation milestone. Confirm that the learner's account authorizes plan usage and that a suitable available model supports the grading contract. Record the tested date, model, SDK version, and result. If eligibility or preview behaviour prevents this, preserve the working local features, report the exact limitation, and keep connection acceptance pending. [Current preview requirements](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)

The application owns its learning history and sends the question, rubric, reference explanation, and relevant attempt as request context. Send each request with `store=false` and `stream=true`. Use the public Responses API endpoint and the account's available models. Sign-in supplies identity and authorized model access. Learning context comes from the local database and stored content. Explain the data sent to OpenAI and link the current applicable data policy during setup. [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)

Implement the documented OAuth flow, which lets the learner authorize the app through OpenAI's browser sign-in. Generate and persist one opaque host ID per installation. Use dynamic client registration on first sign-in and retain the issued client ID for later sign-ins. Start a temporary loopback callback listener on `127.0.0.1` before opening the browser and stop it after completion, cancellation, or timeout. Follow the documented callback path and redirect matching rules. Validate state, the authorization-code proof through PKCE, the ID-token signature, issuer, audience, expiry, nonce, and granted plan-usage scope with maintained authentication libraries. Keep sign-in cancellation recoverable. [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)

Persist the validated account identity, issued client ID, access token, refresh token, granted scopes, and expiry in a protected local credential record. Refresh expired access tokens and atomically replace rotating credentials together. Serialize refreshes across app processes so concurrent tabs or launches preserve a valid connection. Provide sign-out and reconnection controls, plus instructions for revoking the connection in ChatGPT settings. A sign-out removes local credentials and preserves learning data. Credential storage requirements appear in section 12.

The learner can manage connected-app access and a weekly percentage cap in ChatGPT settings. App requests share existing plan limits, and remaining allowance may also depend on other ChatGPT or Codex activity. Explain how to disable use of additional credits after the included allowance is exhausted in ChatGPT's connected-app usage settings. Show a clear pending-grading state when plan usage is unavailable. [Plan usage and app limits](https://developers.openai.com/codex/sign-in-with-chatgpt)

## 3 Daily learning journey

1. The learner starts the app using the documented launcher and sees today's session.
2. The app creates today's session on first access and saves the selected questions immediately.
3. Show one question at a time, its topic, difficulty, and a brief description of the expected scope of the answer.
4. The learner types an answer, saves a draft, requests a hint, views the reference explanation, or chooses “I don't know.”
5. On submission, save the answer before making a grading request.
6. Show a progress indicator while grading and preserve the saved answer if grading fails.
7. Show the assessment and an explanation grounded in the marking guide.
8. Offer one revised answer with feedback and a button to review the concept sooner.
9. Move to another question, with further questions available on demand after the daily minimum is met. Section browsing and unfinished answers remain accessible.
10. Show the day's completed-question count and goal status, alongside concepts practised, any available feedback, and next review dates.

The daily goal is two distinct completed questions. Saving an original answer submission immediately counts the question for that date, regardless of its grade or whether grading is pending or disputed. Choosing “I don't know” counts after the learner views the stored explanation. Drafts, clarification, revisions, reassessments, and duplicate submissions add no extra calendar count for that question on that date. Additional distinct completed questions increase the displayed daily count.

Record zero completed questions as no practice, one as partial, and two or more as goal met. Learning-progress evidence and review scheduling use accepted assessments and recorded support use. Preserve the calendar count when grading later fails or an assessment is disputed.

Reopening, refreshing, or opening a second browser tab resumes saved work and retains the selected questions. Unfinished answers remain editable after midnight. An answer started Monday and submitted Tuesday counts toward Tuesday, while Monday retains its actual count. A missed day leads to the normal daily minimum on return, with due reviews available through the normal selection and section navigation.

## 4 Curriculum and model understanding

For the personal trial, focus on ML fundamentals and introductory deployment and MLOps. Group ML questions into sections such as fundamentals, forests, ensembles, and deep learning. Additional questions and learning areas can follow later. The six domains described below provide an expansion roadmap.

Assign every question one primary concept, one primary section, and one primary domain. Additional tags can describe connected topics. Only the primary concept receives a scheduling update from that answer. Provide visible section browsing and selection, with due reviews available alongside it. As an author-proposed content default, use the primary section for browsing and variety weighting, and tags for overlaps such as forests and ensembles.

### ML fundamentals and model families

The expanded curriculum develops how models work and how to choose between them, alongside evaluation and generalisation. Select a small introductory subset for the trial.

| Area | Required learning coverage |
| --- | --- |
| Learning settings | Supervised, unsupervised, self-supervised, semi-supervised, and reinforcement learning with practical examples |
| Prediction tasks | Regression, classification, ranking, clustering, anomaly detection, and forecasting |
| Linear models | Linear and logistic regression, assumptions, loss functions, regularisation through ridge, lasso, and elastic net |
| Tree models | Decision-tree splits, depth, overfitting, random forests, bagging, and gradient boosting, including what XGBoost and LightGBM implement |
| Similarity and probabilistic models | KNN, distance metrics, scaling, naive Bayes, conditional independence, and SVM margins and kernels |
| Clustering | K-means, hierarchical clustering, DBSCAN, assumptions, noise, and evaluation challenges |
| Dimensionality reduction | PCA, variance, scaling, information loss, and effects on interpretation |
| Neural networks | MLPs, activations, backpropagation, gradient descent, CNNs, RNNs and LSTMs, Transformers, and autoencoders |
| Time series and anomalies | Naive forecasting baselines, ARIMA at a conceptual level, temporal validation, and isolation forests |
| Model evaluation | Baselines, bias and variance, leakage, train/validation/test roles, cross-validation, class imbalance, calibration, and regression and classification metrics |
| Model selection | Data size, feature types, interpretability, predictive quality, training cost, inference latency, memory, and operational constraints |

For a model family, questions should develop an explanation of the mechanism, what is learned during training, how predictions are made, suitable data and tasks, assumptions, preprocessing, failure modes, and trade-offs. Spread these across manageable questions.

Teach the relationships between concepts accurately. For example, logistic regression is commonly used for classification, KNN retains training examples for prediction, and Transformers are a neural-network architecture. A model family can support several tasks depending on its formulation.

### Statistics

Cover probability and conditional probability, Bayes' theorem, distributions, expectation and variance, sampling and selection bias, the law of large numbers and central limit theorem, confidence intervals, hypothesis tests, p-values, effect size, statistical power, correlation and confounding, and basic A/B testing. Pair related ideas in a question where necessary to keep the starter bank manageable.

### Deployment and MLOps

Cover batch and online inference, reproducible preprocessing, model and data versioning, configuration, logging, dependency management, Docker fundamentals, CI and CD concepts, shadow and canary deployment, rollback, training-serving skew, drift, and monitoring both service health and prediction quality. Include the meaning and purpose of Kubernetes at an introductory level.

### AI and LLM applications

Cover tokens and context limits, embeddings, vector similarity, Transformer self-attention, prompting, RAG retrieval and generation, chunking, retrieval quality and answer quality, hallucinations, source grounding, fine-tuning versus retrieval, prompt injection, and privacy. Teach when an LLM application needs additional evaluation beyond a plausible answer.

### Performance and reliability

Cover latency and throughput, p50/p95/p99, measurement and bottleneck diagnosis, caching, batching, concurrency, asynchronous I/O, network and database overhead, timeouts, retries and backoff, idempotency, quantisation, model size, and quality/cost/latency trade-offs. Include both reducing predictive error and reducing operational failures.

### ETL and data engineering

Cover ETL and ELT, batch and streaming, schema and range validation, missing and duplicate data, joins and leakage, timestamps and timezones, incremental processing, watermarks, idempotency, late data, backfills, failed runs, data quality checks, and orchestration fundamentals.

## 5 Content bank and question quality

Ship approximately 12 validated questions for the personal trial, covering selected ML fundamentals and introductory deployment and MLOps concepts. Each question has a focused learning objective and belongs to a visible topic section. Expand the bank after using the trial.

The learner approved the following starter allocation in Q19 and delegated detailed content authoring and source review to implementation. Keep the difficulty introductory, with short explanation and reasoning prompts. Each topic below becomes one question with its own objective, hint, marking guide, explanation, and supporting sources.

| Section | Questions | Confirmed topics |
| --- | --- | --- |
| Fundamentals | 3 | Generalisation, data leakage, evaluation metrics |
| Forests | 2 | How random forests train and predict, how tree diversity helps |
| Other ensembles | 2 | Bagging versus boosting, boosting trade-offs |
| Deep learning | 2 | Backpropagation, overfitting |
| Deployment | 1 | Batch versus online inference |
| MLOps | 2 | Reproducible preprocessing, production monitoring |

The learner can inspect the resulting content in the app and add or change questions later.

Use brief, general-purpose applied examples where they help test understanding. Short Python or SQL fragments may be used for explanation and assessment. Treat submitted code as text.

Store the bank as version-controlled JSON validated with Pydantic. Each record has a stable question ID, content version, primary concept ID, primary section, domain, tags, difficulty, question type, prompt, learning objective, expected answer length, one prewritten hint, reference explanation, weighted rubric criteria, critical misconceptions, and source references with a last-reviewed date. Weights must be positive and sum to one within a documented tolerance. Limit each rubric to eight criteria so assessment remains focused and fits the response budget.

Use established documentation and educational references, including scikit-learn documentation, reputable statistics material, PyTorch documentation, and official infrastructure or model-provider documentation where appropriate. The grader receives stored reference content. Feedback citations come from stored source records. Links must actually support the relevant content.

Validate unique IDs, section membership, starter coverage, concept references, source fields, difficulty values, rubric totals, and all required text at startup and in tests. Source-field checks validate URL syntax, title, source ID, and review-date format. During content authoring, inspect the linked material and record which learning objective it supports in a source-review note. Include those editorial checks in release evidence. Live link checks can run explicitly during content review. Startup and offline tests use local metadata validation. Report invalid records with their ID and a clear reason. Validate a new bank completely before importing it in one transaction.

Updating question content preserves historical question snapshots and stable concept IDs. Retired questions remain available in history. Adding questions preserves existing attempts and review dates.

### Example prompts for curriculum expansion

The following examples illustrate the broader curriculum roadmap. The confirmed trial topics are recorded in the allocation above.

| Domain | Example |
| --- | --- |
| ML models | Explain how a random forest trains and predicts. Why can averaging trees improve generalisation, and when might the model still struggle? |
| ML models | Compare K-means and DBSCAN for data with irregular clusters and noise. What would you inspect before choosing? |
| ML models | Explain what the SVM kernel trick allows the model to compute. How can feature scaling and kernel settings affect its behaviour? |
| Statistics | A 95% confidence interval is reported for a population mean. Explain its interpretation and how correlated observations could affect it. |
| Deployment | A model performs well offline and poorly after deployment. How would you investigate preprocessing differences and data drift? |
| LLM applications | Your RAG application retrieves relevant documents but gives unsupported answers. How would you evaluate retrieval and generation separately? |
| Performance | p95 prediction latency doubles while p50 stays similar. What would you measure, and how would the findings guide a fix? |
| ETL | A daily ingestion job retries after a partial failure. How would you avoid duplicate records and verify completeness? |

## 6 Question selection

Selection runs locally using the reviewed bank and persisted practice history. The initial bank has approximately 12 reviewed questions. Revisit those questions as appropriate, with new reviewed questions added over time.

When a review is due, randomly select one eligible due review for the first suggested question. Choose the second question through a varied selection that favours less recently practised sections and uses a different section when possible. Without a due review, use the varied selection for both questions. Prefer different primary concepts within the daily pair.

Offer additional questions on demand. The learner can continue the varied mix or choose an available section for focused practice. Due reviews remain available alongside section browsing. Use the same reviewed content for first attempts and later reviews.

Daily recommendations support the two-question goal. Completed questions from focused practice or resumed answers also count toward it. Persist selection before displaying it so refreshing, restarting, or opening another tab retains the saved questions. Additional selections are saved when requested.

Avoid duplicate question recommendations within the daily pair. Prefer questions not yet practised that day when alternatives exist. A question with an existing unfinished answer should offer resumption of that answer. A shortage of eligible content is shown explicitly while preserving the daily goal of two distinct questions.

Use the introductory starter bank across its available sections. Section choice changes focused practice. The automatic pair continues to use the saved review/variety policy. More advanced content and eligibility controls can follow bank growth.

Create a daily recommendation record and its two question snapshots atomically with a unique local-date constraint. Save question and marking-guide snapshots with practice items. Attribute calendar completion independently using the date the learner finishes the question, including questions resumed from earlier recommendations.

## 7 Spaced repetition policy

Spaced repetition means reviewing a concept after increasing delays. The learner confirmed the 1, 3, 7, 14, and 30-day progression. A due date makes a concept eligible for the review pool. The daily pair still follows the confirmed review/variety mix.

Maintain a review stage from zero to four per concept. A new concept starts at zero. The following stage mechanics are author-proposed implementation details that make the confirmed outcomes deterministic.

| Original attempt outcome | Scheduling action |
| --- | --- |
| Grade 0 or 1, or “I don't know” | Reset stage to zero and schedule tomorrow |
| Grade 2, mostly correct with an important omission | Reset stage to zero and schedule in three days |
| Grade 3 with independent answering | Schedule using the interval at the current stage, then increment stage to a maximum of four |
| Hint or reference answer viewed before submission | Reset stage to zero and schedule tomorrow, retaining the technical assessment |
| Technical grading failure or assessment requiring clarification | Preserve the existing review state until an accepted assessment is available |

Support takes precedence over a grade-based interval. An independently correct answer to a new concept returns in one day, with later independently correct reviews returning after three, seven, fourteen, and thirty days. Apply intervals to the original submission's local date. Clamp a computed date in the past to the date the feedback is accepted. A self-reported gap uses the explanation-view completion date and needs no grading call.

A revision after feedback is assisted practice and shares the original question's learning event. It records improvement while preserving the original scheduling result. Clarification stays part of the original answer and has one eventual scheduling result. Independent answering describes the help recorded before the original submission. Viewing an explanation afterwards preserves that status.

Process eligible original attempts by submission timestamp in UTC, with a stable attempt ID as a tie-breaker. The first accepted result for a concept on a submission local date follows the table. A later independent grade 3 that day preserves stage and due date. A later grade 2 resets the stage and takes the earlier of the existing due date and its three-day candidate. A later grade 0 or 1, or pre-answer help or self-reported gap, resets the stage and takes the earlier of the existing due date and its tomorrow candidate. Thus extra same-day work can reveal gaps and each concept advances at most once per day.

Save immutable learning evidence and derive the current review state from its eligible events. Recompute the concept in submission order when delayed feedback arrives or a dispute opens or resolves. This makes the result independent of the order in which network replies arrive. Include a submitted answer's assessment only when it is accepted and undisputed. Preserve each original attempt's identity throughout replay.

“Review sooner” records the learner's request and sets the next review to the earlier of its current date and tomorrow. With no current due date, use tomorrow. Preserve requested earlier reviews during recomputation. If a dispute removes the only accepted evidence, show “Awaiting accepted assessment,” clear its evidence-derived review date, preserve a requested earlier review, and keep the concept available in the varied rotation and focused practice.

## 8 Grading contract

Use a four-level assessment scale.

| Grade | Meaning |
| --- | --- |
| 0 | The answer does not demonstrate the core concept or contains a fundamental misconception |
| 1 | Some relevant knowledge is present, with substantial gaps or errors |
| 2 | The central idea is correct, with an important omission, imprecision, or weak application |
| 3 | The answer covers the question's key requirements accurately and gives sound reasoning at the requested level |

Judge the requested learning objective. Accept equivalent terminology, valid alternative approaches, concise correct answers, and explanations in the learner's own words. Assess spelling and grammar only when meaning is genuinely unclear.

The LLM returns criterion assessments as `met`, `partial`, or `not_met`, with a short justification grounded in the answer, and identifies any rubric-defined critical misconception. Compute a weighted coverage score locally using 1, 0.5, and 0 for those statuses. Map coverage below 0.25 to grade 0, below 0.60 to grade 1, below 0.85 to grade 2, and the remainder to grade 3. An identified critical misconception caps the grade at 1. Keep these thresholds in grading configuration and cover boundary values in tests.

The model also returns an assessment status of `assessed` or `needs_clarification`. Use the latter when the question or response cannot be fairly assessed. Show its explanation and retain the answer so the learner can clarify and resubmit. Clarification produces a new immutable submission revision linked to the same original attempt, with one eventual scheduling effect.

The initial feedback shows the grade, a concise summary of what was understood, specific gaps or corrections, and a concise model answer. Expandable teaching detail provides step-by-step reasoning, a brief general-purpose example, and supporting sources. A follow-up question is optional to attempt. Offer one revised answer per original assessed answer, preserving both versions and their feedback. Store reference source IDs and render their stored links.

Display that feedback is AI-generated. Offer “Review sooner” and access to references from the feedback view. Save the complete validated assessment, computed grade, model ID, prompt version, question version, timestamps, request ID when available, latency, and token usage when supplied.

### Assessment disputes

The learner can flag an assessment as incorrect and write a short objection. Retain the original answer, assessment, and objection together. While the dispute is open, exclude that assessment from learning-progress and review-date calculations, using the other undisputed assessments as evidence.

The learner can request reassessment of the saved answer against the question's marking guide and references. Show both assessments and their explanations. The learner can accept either assessment or leave the dispute open. Retain the assessment history after resolution. The reassessment and the original assessment belong to the same answered question. Preserve the question's calendar credit throughout the dispute. When no accepted assessment remains, use the awaiting-assessment rule in section 7. Record the learner-selected resolution and recompute the concept from eligible evidence, treating the selected assessment as the result for the original answer.

Reassessment targets the exact evaluated submission and preserves its help classification and original/assisted-revision role. Resolving a dispute on an assisted revision retains the original answer's scheduling result under section 7.

## 9 LLM request handling

Use the official SDK with the OAuth access token as its bearer credential. Fetch the signed-in account's current model catalog and preserve a configurable selection from the returned models. Verify structured-output support on this plan-authorized route during the first milestone. Prefer a supported JSON-schema response format with a Pydantic schema. If the route lacks schema enforcement, request JSON and validate it with the same Pydantic contract. Accept feedback only after complete schema validation. Validate criterion IDs against the question rubric, require exactly one assessment per criterion, constrain statuses and list sizes, and validate all required feedback text. Render provider refusals and incomplete outputs as recoverable grading failures.

Follow the preview request contract. Send `input` as an array, place tutor instructions in `instructions` or developer messages, and supply the relevant context with every request. Set `store=false` and `stream=true`. Omit unsupported fields, including `max_output_tokens`, `temperature`, `conversation`, and `previous_response_id`. Recheck the current documented field restrictions when implementing. Consume the stream through its terminal event and accept success only on `response.completed` with valid feedback. A failed, incomplete, or interrupted stream preserves the saved answer and leaves grading pending. [Preview requirements](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)

Send explicit tutor instructions, the question, reference explanation, rubric, and answer as separately labelled content. Treat answers and imported material as untrusted data. Tutor instructions must direct the model to assess the answer even if the answer contains instructions to change the grade or reveal internal prompts. Render feedback using escaped React text and safe Markdown with raw HTML disabled.

Persist the submitted answer and a grading-request record before the network call. Use a short transaction to claim a pending request, release the database transaction during the call, then save the result and apply scheduling in one transaction. Conditional state changes and unique constraints must prevent simultaneous tabs from processing the same pending request or applying the same result twice.

Default to a 60-second request timeout and explicit SDK retry configuration. Transient failures may receive one bounded automatic retry for a confirmed temporary rate-limit response, respecting a short Retry-After value. For a timeout or lost connection with an uncertain provider outcome, offer an explicit user retry and explain that the earlier call may already have consumed plan usage. Handle expired authentication with the documented token refresh, and guide the learner to reconnect after revocation or denied permission. Plan-limit exhaustion and plan-usage unavailability require a visible pending state and later retry. Handle `subscription_sharing_usage_limit_exceeded` and `subscription_sharing_usage_unavailable` even when delivered as `response.failed` after streaming begins. Invalid structured output permits an explicit retry. All failures preserve the answer.

On restart, classify interrupted in-flight requests as outcome unknown and make them recoverable through the UI. Store successful feedback so refreshing the screen reuses it. Record each network attempt against the logical grading request.

“I don't know” reveals the stored reference explanation locally. Once viewed, record the completed question and self-reported gap. Schedule that concept tomorrow under the confirmed review policy. Viewing history, choosing questions, saving drafts, and reading references work offline and before sign-in. An answer submitted while the ChatGPT connection is unavailable is saved as awaiting grading and counts toward that day's practice goal. It can be explicitly sent once access is restored. Its assessment and scheduling remain pending.

## 10 Interface requirements

Use React and the pinned MagicPath visual design for Today, simple History, and necessary Settings. Include the practice calendar in Today. Maintain readable contrast, visible labels, keyboard navigation, and feedback status conveyed through text and colour.

Today shows the active question, section, saved-draft status, answer form, hint control, submission status, feedback, and the actual daily count, such as “1 of 2 questions completed today.” Keep more-question and section-selection controls reachable. Offer saved unfinished answers for resumption across days. Save drafts explicitly and on question navigation, and state the save behaviour near the form. A failed save retains the editor text with copy and retry controls. A separate “Show explanation” control reveals the stored reference answer. Persist support use before revealing help and snapshot it on submission.

The practice calendar shows actual completed-question counts and a text legend for no practice, partial, and goal met. Pending grading and assessment disputes preserve calendar credit. As an author-proposed display default, leave future dates and dates before first use unmarked, and describe today's zero count as no practice yet. Use colour alongside text or accessible date labels.

Feedback uses the concise-first presentation in section 8. Show one revision, “Review sooner,” sources, and the assessment-dispute flow. For an open dispute, show its status and both available assessments. Preserve access to other questions while a grading request is running.

History shows the exact question version, original answer, feedback, help used, clarification or revision, completion date, assessment date, review date, and dispute history. Support section and date filters. Link unfinished answers back to their editable practice item and pending assessments to their recovery action.

Settings provides the configured timezone, available model selection, connection controls, local request counts, and backup action. Show the connected account, whether plan usage was authorized, connection status, and sign-in, sign-out, and reconnect controls as appropriate. An explicit connection-test button states that it consumes plan usage. Link to ChatGPT settings for connected-app usage controls and revocation. Exact provider allowance is shown only when supplied by a documented response.

Full Progress analytics and reminder controls belong to later expansion. Proposed analytics include practice by domain, due concepts, assessment distributions with sample counts, and concepts recalled independently on at least two dates, with links to supporting attempts.

## 11 Storage and data ownership

The default user-data directory is `~/Library/Application Support/Daily ML Practice/`, resolved programmatically and overridable in configuration. Keep the SQLite database, logs, and user settings there. Keep shipped question content in package resources. Use filesystem paths safely when they contain spaces.

Use a small schema with clear ownership. The implementing agent may adjust table boundaries while preserving these records and invariants.

- Questions and concepts with stable IDs, content versions, domain membership, and active status.
- Daily sessions with local date, timezone, status, and creation timestamp.
- Practice items linked to their original recommendation, with immutable question/rubric snapshots, resumable drafts, and support-use flags.
- Attempts with immutable submitted text, original/revision role, submission revision, and timestamps.
- Completed-question records with question identity, completion timestamp, local date, and timezone, including explanation-view completion for self-reported gaps.
- Grading requests and network attempts with state, provider metadata, failure category, and usage.
- Evaluations with validated structured feedback and locally computed assessment.
- Concept review state with stage, due date, and latest applied scheduling event.
- Settings and schema version.

Enforce one session per local date, one slot per session, foreign keys, and at-most-once application of an attempt's scheduling effect. Use parameterised SQL, bounded database lock waits, and short transactions. Concurrent updates that lose a conditional claim reload the saved state and show it.

Store timestamps in UTC and preserve the local date and timezone recorded for recommendations and completion events. Handle Europe/London daylight-saving transitions using zoneinfo. On rollover, prepare today's recommendations while retaining editable unfinished answers and their question snapshots. New submissions, hints, explanations, revisions, and grading recovery remain available for resumed work.

Credit an original answer to its saved submission date, even when grading arrives later. Credit a self-reported gap to the date its explanation is viewed. Enforce one calendar credit per question per local date. As an author-proposed date default, use the configured learning timezone effective at completion and save it with the event. A later timezone change preserves existing calendar records. Recheck the completion date when saving, including from a browser tab left open overnight.

Provide an in-app “Back up learning data” action using SQLite's supported backup mechanism. The backup contains a schema version, all learning records and settings, and stored question snapshots. Provide a documented restore command that validates the backup in a temporary location, creates a pre-restore backup, and restores atomically while the app is stopped. Exclude sign-in credentials and tokens from backup content.

Apply schema upgrades transactionally after a verified pre-upgrade backup. On validation or migration failure, preserve existing data and explain the recovery path.

## 12 Configuration, privacy, and usage

Use validated configuration for paths, timezone, review intervals, grading thresholds, model selection, input limits, and request timeout. As an author-proposed simplification, load packaged TOML defaults, saved SQLite preferences, installation TOML overrides, then environment overrides in increasing precedence. Resolve the data-directory location from installation configuration before loading that database's preferences. Store editable user settings in SQLite with learning data, committing settings and their version atomically. Keep defaults small and documented. This gives backup and restore one authoritative data file to preserve. Existing recommendation and completion records retain their date, timezone, and question snapshots.

Keep the OAuth credential record outside the learning database, logs, configuration examples, and backups, in an app-owned directory with owner-only permissions. Use directory mode `0700` and file mode `0600` on macOS, atomic replacement, and a cross-process refresh lock. Exclude credential files from version control and use placeholder-only documentation examples.

The author-proposed answer limit is 8,000 Unicode code points and the model-request deadline is 60 seconds. Configure SDK retries explicitly. A request timeout bounds waiting time. Response-field validation bounds accepted feedback. Follow the current preview's supported request fields, including its output-token restrictions.

ChatGPT plan controls govern grading availability. Persist UTC timestamps for each outbound model attempt and show the app's rolling 24-hour request count for visibility. Include connection tests, retries, revisions, reassessments, and clarification submissions. Authentication, refresh, model catalog reads, and reuse of stored feedback have their own request records where useful. Local request counts describe observed app activity. Display exact remaining plan allowance only when a documented provider response supplies it.

Preserve submitted answers and calendar credit when plan access or allowance prevents grading. Show the pending reason and an explicit send or retry action. Link to ChatGPT's connected-app usage and credit controls during setup and in Settings. The learner selected the plan-authorized route. Any change to a separately billed connection needs the learner's explicit approval.

Bind the FastAPI server to `127.0.0.1`. Serve React assets and the API from the same origin, validate Host and Origin, and require per-instance same-origin request protection on mutations. The submission notice explains what is sent to OpenAI. Keep learner answers in SQLite and send only the question, marking guide, reference material, and relevant answer or objection needed for the requested assessment.

Use rotating Python logs for operational metadata, request state, redacted error category, model version, latency, and supplied usage. Protect raw answers, tokens, authorization codes, headers, and callback queries from log and error-message disclosure. Render code as text and allow safe link schemes in Markdown.

## 13 Mac startup and later reminders

Provide a documented launcher that starts the installed environment, opens the browser, and gives a clear way to stop the app. Use Python 3.12 or a newer mutually supported version selected and recorded during implementation. Resolve the interpreter, package assets, and data paths explicitly so launching outside the project directory works. A port conflict produces a clear message or selects an available loopback port.

The trial starts through this launcher. A later optional macOS reminder can use a uniquely named per-user launchd agent and a short-lived notification helper. That expansion requires explicit Settings enablement, enable/test/disable controls, visible permission and installation recovery, and actual-Mac delivery verification. Reminder work follows trial feedback.

## 14 Architecture and engineering standards

Use React, TypeScript, Vite, and the MagicPath component's CSS and Lucide icons for the interface. Use FastAPI and Pydantic for the browser API, Python's sqlite3 for durable learning records, and the official OpenAI SDK with maintained OAuth/OIDC/JWT libraries for the documented sign-in route. Resolve compatible versions during implementation and record reproducible Python and frontend dependency locks.

A single Python process serves compiled frontend assets and `/api/*` on loopback. Use an app-instance lock for one owner per data directory and one server worker. A small in-process worker handles persisted grading requests. It releases database transactions during network calls. Browser state holds temporary editing state and reads saved learning state from Python. Credentials remain in the backend.

Keep clear responsibilities for configuration, content validation, question selection, practice and submissions, grading, review scheduling, persistence, connection handling, reports, and backup. Reuse Python's standard library for paths, SQLite, timezones, logging, and TOML. Add files when a responsibility needs them. The trial has Today, History, and Settings views using simple hash navigation and native fetch.

Add module, class, and public-function docstrings. Use NumPy-style Parameters, Returns, and Raises sections where relevant. Catch specific exceptions where recovery is possible, translate them into helpful UI messages, and log safe diagnostic metadata. Keep scheduling and grade computation independent of presentation and network calls.

Validate content and model responses at their trust boundaries. Content validation checks an entire bank before atomic import. Response validation accepts feedback only after the stream completes and the full rubric-based payload validates. Short SQLite transactions, foreign keys, conditional draft versions, unique completion keys, and request claims protect simultaneous tabs and repeated actions.

## 15 Verification and acceptance criteria

Use pytest for Python tests, Vitest and Testing Library for interface behaviour, and Playwright for normal browser flows. Each new Python function or class needs happy-path and edge/error coverage under the workspace rules. Explain Arrange, Act, Assert when first introducing tests. Default tests use mocked providers, isolated temporary data, controlled clocks, and injected randomness. Test output types, null and missing values, complete criterion coverage, grade ranges, and exact grade boundaries.

| ID | Required trial proof |
| --- | --- |
| AC01 | A clean macOS setup follows the README and opens compiled React assets and the API on loopback through the documented launcher |
| AC02 | Approximately 12 reviewed questions pass schema, section, starter-coverage, rubric, and supporting-source checks |
| AC03 | First access saves the random review/variety pair, refresh/restart/two tabs retain it, and additional or focused questions remain available |
| AC04 | Submitting an answer yields concise-first criterion-based feedback, expandable teaching detail, and a locally calculated 0–3 grade |
| AC05 | Correct paraphrases, concise answers, and valid alternative solutions receive appropriate credit |
| AC06 | Rubric-defined critical misconceptions are identified and cap the grade at 1 |
| AC07 | The 1/3/7/14/30 progression, three-day mostly-correct result, next-day gaps/help, same-day advancement limit, and delayed-result replay follow section 7 |
| AC08 | Two tabs and repeated actions create one original submission, one calendar credit, and one eligible scheduling effect |
| AC09 | Sign-in, timeout, rate-limit, plan-limit, malformed-output, refusal, interrupted-stream, and restart failures preserve answers and show recovery |
| AC10 | Offline selection, drafts, stored explanations, history, and calendar work, with submitted answers saved awaiting grading |
| AC11 | Cross-day drafts remain editable, completion credits the correct date, and timezone changes and Europe/London DST preserve existing records |
| AC12 | One assisted revision is linked to the original answer, preserves its scheduling result, and adds no calendar credit |
| AC13 | Simple history distinguishes independent and assisted attempts and exposes the saved evidence and feedback |
| AC14 | Backup/restore preserves all learning records and editable settings, with verified pre-change backups and failure-safe restore/migration |
| AC15 | Local request counts include every outbound grading/test/retry/reassessment attempt and plan-usage failures leave saved work recoverable |
| AC16 | A bank update or question retirement preserves historical snapshots and existing learning evidence |
| AC18 | Operational logs and source-control checks protect secrets and raw answer text and use placeholder-only examples |
| AC19 | On the learner's Mac, sign-in authorizes plan usage, reads account models, and completes real validated grading with tested versions recorded |
| AC20 | Callback identity/scope, token refresh/rotation, sign-out/reconnect, credential protection, and secret-free backups pass targeted checks |
| AC21 | Requests follow the current preview contract and feedback commits only after response.completed and complete validation |
| AC22 | Calendar shows 0/1/2-or-more and actual counts, retains pending/disputed credit, counts viewed self-reported gaps, and rejects duplicate credit |
| AC23 | Disputes preserve all records, exclude disputed evidence, support reassessment and learner-selected resolution, and handle the sole-assessment case |

AC17 belongs to the later reminder expansion and requires actual-Mac notification evidence when that work is commissioned.

Create at least 18 fixed grader cases drawn from the trial bank. Include fully correct, mostly correct, substantial-gap, critical-misconception, paraphrase, valid-alternative, clarification, and grading-manipulation cases. Define expected grades or narrow ranges and required corrections from the reviewed marking guides before running the model. Keep evaluation data and a resumable runner.

Run the live evaluation explicitly through the authorized plan connection. Record model, prompt version, question versions, all completed cases, agreement, and each mismatch. The proposed release target is at least 16 of 18 cases in range and every predefined critical misconception identified. This is a small release check. Record its sample size and limitations. Preserve partial runs across temporary plan unavailability.

Record exact commands and results for the offline tests, content validation, frontend checks, browser flows, real sign-in/refresh, live grader evaluation, clean-Mac startup, and backup/restore. A live check awaiting access remains pending. The Jira completion rules apply to all required trial checks.

## 16 Implementation sequence and deliverables

Deliver small working slices through the normal app entry point.

1. Prove documented ChatGPT plan sign-in, protected credentials and refresh, account model lookup, and one complete validated grading request.
2. Import the reviewed starter bank and create durable random recommendations, resumable practice items, and cross-day drafts in SQLite.
3. Match MagicPath Today with real draft saving, answer submission, calendar counts, pending grading, and recovery.
4. Complete feedback, hints, self-reported gaps, one revision, ordered review scheduling, and assessment disputes.
5. Add simple History, necessary Settings, backup and restore, and safe content updates.
6. Verify the complete personal trial through offline tests, browser flows, real-account grading evaluation, and macOS setup and recovery.

Deliver the application source, approximately 12 reviewed questions, configuration examples, dependency locks, targeted tests, evaluation fixtures and runner, README, and actual verification evidence. The README covers setup, sign-in and consent, plan usage controls, running and stopping, data and credentials, draft saving, review rules, disputes, content additions, backup/restore, and recovery.

After each meaningful implementation step, explain the change and its reason in plain English. Define new software engineering terms and connect them to the learner's data science knowledge where helpful. More content, detailed analytics, reminders, and deeper engineering or LLM curricula follow trial feedback.

## 17 Instructions for the implementing Codex

Read this specification and all applicable AGENTS.md instructions before editing. Inspect any existing project before deciding where files belong. Treat the technical defaults in this document as the approved design, and seek approval for material changes affecting privacy, billing, data safety, or the chosen product experience.

Associate implementation with a real Jira issue before changing code, following the user's workspace lifecycle. Put the approved scope and acceptance criteria into that issue. Collect the implementation summary, exact verification commands and results, and material limitations for the Jira completion comment. A successful comment and confirmed Done transition are required for completion. Follow the workspace checkpoint and next-issue handoff rules when their conditions apply.

Prepare a concrete implementation plan from this specification, then implement incrementally with the required tests. Use the smallest sufficient verification for each step and the release checks in section 15. Preserve any concurrent work and finish by reporting delivered behaviour, verification evidence, and pending material limitations.
