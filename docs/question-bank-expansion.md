# Daily ML question bank expansion

50 candidate questions for the six sections in the current Daily ML curriculum. The prompts and scenarios are original. Each question includes a suggested difficulty, the points a strong answer should cover, and a primary source for those points. Sourcing checked on 26 November 2026.

The grading notes are for content review. The app’s release format also requires a hint, a reference explanation, weighted criteria, and critical misconceptions for each question. See the [starter question source review](content-review.md) for the existing format.

## Fundamentals

1. **A baseline for crowd counts** (Introductory)  
   You’ve trained a model to estimate venue occupancy. What constant predictor would you use as a baseline if you score it with mean absolute error, and how would you make the comparison fair?  
   Assess using the median training count and evaluating both models on the same held-out rows with the same metric. [scikit-learn — DummyRegressor](https://scikit-learn.org/stable/modules/generated/sklearn.dummy.DummyRegressor.html)

2. **Holding out whole venues** (Intermediate)  
   You have thousands of Wi-Fi readings from 12 venues. The model will be deployed in a 13th venue. How would you assign rows to validation folds to estimate performance for that launch?  
   Assess grouping by venue, keeping each venue entirely on one side of a fold, and explaining why a row-wise split answers a different question. [scikit-learn — GroupKFold](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.GroupKFold.html)

3. **Validating a forecast in time order** (Intermediate)  
   At the end of each day you forecast the next day’s occupancy. You have one year of daily data. How would you design several validation folds, and when would you leave a gap before a validation block?  
   Assess training on earlier data and scoring on later blocks. Acknowledge the gap when labels or features near the boundary would otherwise depend on unavailable observations. [scikit-learn — TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)

4. **Estimating performance after tuning** (Advanced)  
   You tried dozens of hyperparameter settings and want to report the winning setting’s cross-validation score. If the available data must also support evaluation, how would nested cross-validation change what you report?  
   Assess selecting settings within each outer training split, evaluating the resulting fitted model on the untouched outer fold, and aggregating outer scores to estimate the performance of the tuning procedure. [scikit-learn — Nested versus non-nested cross-validation](https://scikit-learn.org/stable/auto_examples/model_selection/plot_nested_cross_validation_iris.html)

5. **Choosing MAE or RMSE** (Introductory)  
   Two crowd-count models typically miss by a few people, but one sometimes misses by 100. Which metric would put more emphasis on those large misses, and what units would the metric use?  
   Assess that root mean squared error (RMSE) gives larger errors more influence than mean absolute error (MAE) through squaring, and both metrics are measured in people. [scikit-learn — Regression metrics](https://scikit-learn.org/stable/modules/model_evaluation.html#regression-metrics)

6. **Checking probability calibration** (Intermediate)  
   Among parcels assigned an 80% chance of arriving late, about 50% arrive late in a representative evaluation sample. What does this suggest, and how would you check the pattern across the range of scores?  
   Assess overconfidence in that score range and using a reliability diagram to compare each bin’s mean predicted probability with its observed event frequency. [scikit-learn — Probability calibration](https://scikit-learn.org/stable/modules/calibration.html#calibration-curves)

7. **Choosing when to raise an alert** (Intermediate)  
   Missing an overcrowding event is costly, but too many false alerts overwhelm staff. You can change the alert threshold on a trained classifier. How would you select and evaluate a threshold?  
   Assess sweeping thresholds on validation data against an explicit cost or alert constraint, measuring the chosen rule on held-out data, and recognising that thresholding changes decisions while leaving the scores available for evaluation. [scikit-learn — Tuning the decision threshold](https://scikit-learn.org/stable/modules/classification_threshold.html)

8. **Macro versus micro F1** (Intermediate)  
   In a single-label classifier with three occupancy bands, the busiest band is rare and poorly predicted. Why might micro F1 look much better than macro F1, and which would you report if each band matters equally?  
   Assess pooling true and false positives and false negatives before calculating micro F1, versus giving every class’s F1 equal weight in the macro average. [scikit-learn — Multiclass classification metrics](https://scikit-learn.org/stable/modules/model_evaluation.html#multiclass-and-multilabel-classification)

9. **An interval for a future count** (Intermediate)  
   A model gives a lower and upper bound for tomorrow’s headcount at each venue and claims 90% coverage. How would you check that claim on held-out days, and what might an overall coverage figure miss?  
   Assess the fraction of realised counts within their own bounds, the value of inspecting interval width, and the possibility that pooled coverage hides poor coverage for busy venues. [scikit-learn — Prediction intervals for gradient boosting regression](https://scikit-learn.org/stable/auto_examples/ensemble/plot_gradient_boosting_quantile.html)

10. **Interpreting a logistic regression coefficient** (Intermediate)  
    A logistic regression model predicts subscription cancellations. The coefficient on the number of support tickets is 0.4. Holding the other features fixed, what does one additional ticket change about the model’s estimated log-odds and odds of cancellation?  
    Assess adding 0.4 to the log-odds and multiplying the odds by exp(0.4), approximately 1.49. The change in predicted probability depends on the starting odds. [scikit-learn — Logistic regression](https://scikit-learn.org/stable/modules/linear_model.html#logistic-regression)

## Forests

11. **Out-of-bag evaluation** (Intermediate)  
    A random forest classifier trained with bootstrap sampling reports an out-of-bag score. For a particular training row, which trees contribute to its out-of-bag prediction and why?  
    Assess selecting only trees whose bootstrap samples omitted that row, aggregating their predictions and comparing aggregated predictions with known labels. [scikit-learn — OOB errors for random forests](https://scikit-learn.org/stable/auto_examples/ensemble/plot_ensemble_oob.html)

12. **Choosing the number of trees** (Intermediate)  
    The out-of-bag error is nearly flat from 300 to 1,000 trees, and you have a tight serving budget. How would you choose a tree count before release?  
    Assess shortlisting counts near the error plateau, checking predictive quality on representative held-out data and measuring inference latency or memory to judge the trade-off. [scikit-learn — OOB errors for random forests](https://scikit-learn.org/stable/auto_examples/ensemble/plot_ensemble_oob.html), [scikit-learn — Computational performance](https://scikit-learn.org/stable/computing/computational_performance.html)

13. **Minimum samples per leaf** (Introductory)  
    A forest regressor produces jagged estimates for nearby sensor readings. What changes inside each tree if you raise `min_samples_leaf`, and when might raising it too far hurt?  
    Assess imposing more training rows per terminal prediction, smoothing highly local patterns, and possibly losing real structure through underfitting. [scikit-learn — RandomForestRegressor](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.RandomForestRegressor.html)

14. **A misleading importance ranking** (Intermediate)  
    A random forest gives a randomly assigned numeric record ID higher impurity-based importance than a meaningful sensor feature. Why can this happen, and how would you check whether the ID helps performance on unseen records?  
    Assess that high-cardinality variables offer many spurious split opportunities and that a held-out permutation test or a comparison after dropping the ID can probe its predictive contribution. [scikit-learn — Permutation importance versus impurity importance](https://scikit-learn.org/stable/auto_examples/inspection/plot_permutation_importance.html)

15. **Importance with correlated sensors** (Advanced)  
    Two sensors produce almost interchangeable readings. Permuting either column on its own barely changes the held-out score. How can both columns still matter, and what follow-up would test their contribution together?  
    Assess that either column can supply overlapping information. A suitable follow-up jointly permutes the pair with the same row permutation or compares a refitted model without either feature on the same held-out set. [scikit-learn — Permutation importance with correlated features](https://scikit-learn.org/stable/auto_examples/inspection/plot_permutation_importance_multicollinear.html)

16. **Class weights in a forest** (Intermediate)  
    A forest classifier misses too many rare high-density windows. What does switching to `class_weight="balanced"` change during fitting, and how would you check whether it helped?  
    Assess giving less frequent labels greater influence through inverse-frequency weights and checking the operational precision and recall trade-off on representative held-out labels. [scikit-learn — RandomForestClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.RandomForestClassifier.html)

17. **Predicting beyond observed crowd counts** (Intermediate)  
    You train a standard squared-error random forest on counts no greater than 500. Can it predict 700 at a larger venue using the same fitted model? Explain using the prediction made by each leaf.  
    Assess that each leaf predicts a mean of training targets and the forest averages those means, so its predictions stay within the observed target range. [scikit-learn — DecisionTreeRegressor](https://scikit-learn.org/stable/modules/generated/sklearn.tree.DecisionTreeRegressor.html), [scikit-learn — RandomForestRegressor](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.RandomForestRegressor.html)

## Other ensembles

18. **Hard versus soft voting** (Introductory)  
    Three binary classifiers give a positive-class probability of 0.51, 0.51 and 0.10 for the same row. With equal weights and each classifier using a 0.5 cutoff, what would hard voting choose? What would soft voting choose?  
    Assess that two individual votes give hard voting a positive result while the mean positive probability of about 0.373 gives soft voting a negative result. [scikit-learn — VotingClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.VotingClassifier.html)

19. **Training a stacking model** (Advanced)  
    You want a second model to combine predictions from a forest and a logistic regression. How would you produce the training inputs for that second model using only the training set?  
    Assess supplying cross-validated predictions for each training row from base models that did not fit that row, then fitting each base model on all training rows for future predictions. [scikit-learn — StackingClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.StackingClassifier.html)

20. **What AdaBoost does after a miss** (Introductory)  
    The first weak classifier in AdaBoost misclassifies several rows. What changes about those rows when training the next classifier, and why?  
    Assess increasing the relative influence of misclassified rows so the next learner pays more attention to harder examples. [scikit-learn — AdaBoostClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.AdaBoostClassifier.html)

21. **Histogram-based split search** (Intermediate)  
    You are fitting boosted trees to millions of rows with high-resolution signal measurements. How does putting numerical values into bins speed up training, and what can coarse bins make harder to learn?  
    Assess searching splits over bin boundaries and the loss of candidate thresholds when values are assigned to the same bin. [scikit-learn — Histogram-based gradient boosting](https://scikit-learn.org/stable/modules/ensemble.html#histogram-based-gradient-boosting)

22. **Learning a route for missing signals** (Intermediate)  
    A scanner’s battery readings are sometimes missing both in training and at prediction time. How can scikit-learn’s histogram gradient boosting route a missing reading at a tree split, and what would you check after a scanner firmware update?  
    Assess learning which child gets missing values based on training split gain and checking whether a change in missingness alters error rates for affected records. [scikit-learn — HistGradientBoostingClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html)

23. **Monotonic predictions** (Intermediate)  
    A pricing model must predict an equal or higher cost for a longer delivery distance when the other inputs stay fixed. How could you enforce that behaviour in a boosted tree model, and how would you test it?  
    Assess an increasing monotonic constraint on distance and comparing predictions of otherwise identical examples across increasing distances. [scikit-learn — Monotonic constraints](https://scikit-learn.org/stable/auto_examples/ensemble/plot_monotonic_constraints.html)

24. **Unordered categories in boosted trees** (Intermediate)  
    You encode scanner models A, B, C and D as the integers 0 to 3. A tree treats that column as numeric. What limitation does the arbitrary ordering impose on a single split, and how does native categorical handling help?  
    Assess that numeric thresholds partition consecutive codes and that categorical splits can group values without relying on the imposed order. [scikit-learn — Categorical feature support in gradient boosting](https://scikit-learn.org/stable/auto_examples/ensemble/plot_gradient_boosting_categorical.html)

25. **Extra Trees and split randomisation** (Intermediate)  
    A colleague says scikit-learn’s Extra Trees and random forests choose their split thresholds the same way. Explain the difference at a node and say whether bootstrap sampling is enabled by default in Extra Trees.  
    Assess random candidate thresholds and selection among those candidates for Extra Trees, best-threshold search over the sampled features for a random forest, and bootstrap sampling being off by default for Extra Trees. [scikit-learn — ExtraTreesClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.ExtraTreesClassifier.html), [scikit-learn — RandomForestClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.RandomForestClassifier.html)

## Deep learning

26. **Fine-tune or freeze?** (Introductory)  
    A team has 1,200 labelled product images for a defect classifier and starts with an image model pretrained on a larger dataset. What changes during training if you freeze the backbone or fine-tune the whole network? What validation evidence would guide your choice?  
    Assess training only a new classifier head with a frozen backbone, updating the pretrained weights during full fine-tuning, and grounding the choice in dataset size, domain similarity and held-out results. [PyTorch — Transfer learning for computer vision](https://docs.pytorch.org/tutorials/beginner/transfer_learning_tutorial.html)

27. **Recoverable training checkpoint** (Intermediate)  
    A GPU training job for a crowd-density model is interrupted after six hours. The team saved only `model.state_dict()`. What additional state should a checkpoint preserve if they want a meaningful resume, and why can restoring weights alone change the remainder of training?  
    Assess preserving the optimiser state and training step, plus scheduler and gradient-scaler state when used. Explain how momentum or adaptive statistics affect the next update. [PyTorch — Saving and loading models](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html), [PyTorch — Automatic mixed precision recipe](https://docs.pytorch.org/tutorials/recipes/recipes/amp_recipe.html)

28. **Mixed precision without blind trust** (Intermediate)  
    Switching a signal-classification training run from FP32 to automatic mixed precision makes iterations faster, but a few runs suddenly produce non-finite loss. Explain what autocasting and gradient scaling are trying to achieve, and give a safe investigation or response before accepting the faster run.  
    Assess autocast choosing precision per operation and gradient scaling reducing underflow for small gradients. Check where non-finite values first appear and compare validation quality with the full-precision run. [PyTorch — Automatic mixed precision](https://docs.pytorch.org/docs/stable/amp.html)

29. **From class logits to loss and probabilities** (Introductory)  
    A network produces three raw class scores per example and the training loop uses PyTorch’s `CrossEntropyLoss` with integer class labels. Where should softmax be applied if training needs a loss and inference needs class probabilities?  
    Assess passing raw logits and integer class indices to the loss, which already applies log-softmax, and applying softmax over the class dimension when probabilities are needed at inference. [PyTorch — CrossEntropyLoss](https://docs.pytorch.org/docs/stable/generated/torch.nn.CrossEntropyLoss.html)

30. **Attention for irregular sensor windows** (Intermediate)  
    A transformer sees Bluetooth signal readings taken at irregular intervals. Why should its input include position or elapsed-time information? Give an example of two temporal relationships different attention heads could learn.  
    Assess that self-attention without positional signals or an order-dependent mask has no inherent sense of order or time gaps. Different heads could, for example, attend to nearby changes and to similar readings further back. [Vaswani et al. — Attention Is All You Need, sections 3.2 and 3.5](https://arxiv.org/abs/1706.03762)

31. **Masking padded timesteps** (Intermediate)  
    Each device trace has a different length, so the data loader pads shorter traces with zeros. A transformer begins associating long padding tails with “low occupancy.” What should the input pipeline and attention computation do so padding does not become evidence?  
    Assess building a mask from real sequence lengths, excluding padded keys from attention, and excluding padded outputs from pooling if the classifier pools over timesteps. A genuine zero reading remains valid. [PyTorch — MultiheadAttention](https://docs.pytorch.org/docs/stable/generated/torch.nn.MultiheadAttention.html)

32. **Scale multi-GPU training correctly** (Advanced)  
    A model is moved from one GPU to four with DistributedDataParallel, but every process sees the same training rows in the same order. What work is being duplicated, and how would you distribute training examples across the workers?  
    Assess spotting repeated forward and backward computation on the same batches. Assign each worker its share with a distributed sampler and explain how gradient synchronisation combines work across workers. [PyTorch — DistributedDataParallel](https://docs.pytorch.org/docs/stable/generated/torch.nn.parallel.DistributedDataParallel.html)

33. **Data loader is the bottleneck** (Intermediate)  
    GPU utilisation is low while a CNN trains on thousands of stored radio-signal spectrograms. Before changing the model, what data-loading measurements and DataLoader settings would you investigate, and why might pinned memory help but not solve every bottleneck?  
    Assess measuring loading, transfer and GPU compute separately, testing workers or prefetching when the loader is slow, and explaining how pinned host memory can speed CPU-to-GPU transfer. [PyTorch — Data loading optimisation](https://docs.pytorch.org/tutorials/intermediate/intermediate_data_loading_tutorial.html)

34. **Smaller student for an edge gateway** (Advanced)  
    A highly accurate occupancy teacher model is too slow for an edge gateway. Propose a knowledge-distillation experiment that can preserve useful behaviour in a smaller student, and name one comparison needed before replacing the teacher.  
    Assess training a smaller student on teacher probabilities and task labels. Compare held-out quality and edge latency against both the teacher and a student trained on labels alone. [PyTorch — Knowledge distillation](https://docs.pytorch.org/tutorials/beginner/knowledge_distillation_tutorial.html)

## Deployment

35. **Contract before traffic** (Introductory)  
    A new model API expects RSSI values in dBm and a `device_count` integer. A mobile client sends a string count and occasionally supplies values in watts. What should the service validate at its boundary, and what should it return or log when the request violates the contract?  
    Assess validating required fields, types and plausible ranges before inference. Units need an explicit contract or an input field because type checks alone cannot detect them. Give a clear client error and record the failing field or rule without logging the raw request. [MLflow — Model signatures and input examples](https://mlflow.org/docs/latest/ml/model/signatures/)

36. **Readiness and liveness probes** (Intermediate)  
    A container has started, but its model weights are still downloading and warming up. What should its readiness and liveness probes each check, and how should they behave during warm-up?  
    Assess gating traffic on model readiness and using liveness to detect a stuck process that needs a restart. A startup probe can delay liveness checks during initialisation. [Kubernetes — Liveness, readiness and startup probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/)

37. **Canary with rollback criteria** (Intermediate)  
    A revised occupancy model has passed offline evaluation. Design a small-traffic canary release that limits user impact. Which operational and model-specific signals would you compare with the stable version, and what would trigger rollback?  
    Assess sending a small fraction of live traffic to the candidate, comparing error rates and latency with the stable version, and checking model quality when labels arrive. Define the threshold for restoring stable traffic in advance. [Kubernetes — Managing workloads](https://kubernetes.io/docs/concepts/workloads/management/)

38. **Safe rolling replacement** (Intermediate)  
    A prediction service has three replicas and must remain available while moving to a new container image. Explain how a rolling update and readiness gating cooperate, and why keeping a previous revision matters.  
    Assess bringing up new replicas in stages, limiting the number unavailable during replacement, sending traffic to ready replicas and restoring a previous revision if the update fails. [Kubernetes — Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)

39. **Autoscaling from a real bottleneck** (Intermediate)  
    Midday requests make a crowd-analytics API exceed its latency target, while CPU use is only 35% because requests mostly wait on a feature service. What would you measure before adding replicas, and how would you protect the feature service?  
    Assess using in-flight requests or queue depth to see where load accumulates, checking whether extra replicas would help given the feature service’s capacity, and bounding replica count to protect that dependency. [Kubernetes — Horizontal Pod Autoscaling](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/)

40. **Memory requests and limits** (Intermediate)  
    A model-serving pod consumes 1.5 GB while loading and 700 MB when warm. Its manifest has no memory request or limit. Neighbouring pods sometimes fail under load. How would you size its memory settings, and what failure mode would you test?  
    Assess using requests for scheduling and limits to cap usage, sizing each with evidence from warm-up and peak usage, and testing whether a restart or out-of-memory kill occurs under load. [Kubernetes — Resource management for pods and containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)

41. **Versioned prediction audit trail** (Introductory)  
    A facilities team disputes a crowd alert sent last Tuesday. What minimum prediction record should let an engineer reconstruct which release acted, without storing raw device identifiers unnecessarily?  
    Assess recording a prediction ID and timestamp, deployment or model version, feature version, model output and threshold or decision-rule version. Retain the operational fields needed to link the alert and its outcome. [Google Cloud — AI and ML reliability](https://docs.cloud.google.com/architecture/framework/perspectives/ai-ml/reliability)

42. **Evaluating with shadow traffic** (Advanced)  
    A team wants to send copies of production requests to a candidate model while continuing to show the stable model’s answer. What could you learn from this shadow deployment? Which conclusions require observed outcomes, and how would you isolate the live response?  
    Assess mirroring traffic while serving the stable response, isolating candidate timeouts and side effects, and comparing disagreements and resource use. Realised outcomes are required to judge accuracy directly. [Istio — Mirroring](https://istio.io/latest/docs/tasks/traffic-management/mirroring/), [Google Cloud — MLOps continuous delivery and automation pipelines](https://docs.cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning)

## MLOps

43. **Promoting a registered model** (Introductory)  
    Two `model.pkl` files are both called “final.” Describe what a model registry should retain and how an explicit production alias or approval state makes promotion safer than copying a file into a serving folder.  
    Assess storing versioned artifacts with their producing runs and metadata, moving the production alias to an approved version, and retaining the prior target for rollback. [MLflow — Model Registry](https://mlflow.org/docs/latest/ml/model-registry/)

44. **Compare experiments fairly** (Introductory)  
    An engineer claims a new crowd-count estimator is better after changing the feature window, random seed, model depth and threshold in one run. What should experiment tracking record, and how would you design the next comparison to attribute an observed improvement?  
    Assess recording parameters, code and data versions, metrics and artifacts. Keep evaluation data and metrics fixed, and change one factor or use a planned design that can distinguish their contributions. [MLflow — Experiment tracking](https://mlflow.org/docs/latest/ml/tracking/)

45. **Data-quality gate before training** (Intermediate)  
    A vendor changes a gateway firmware field and yesterday’s ingest produces 40% missing RSSI values. Where should this be caught in an automated training pipeline, and what should happen to the candidate model if the data check fails?  
    Assess checking fields, ranges and missingness before training, reporting the failed rule and affected data, and stopping the run before it produces a promotable model. [Google Cloud — MLOps continuous delivery and automation pipelines](https://docs.cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning)

46. **Testing a feature change in CI** (Intermediate)  
    A pull request changes how five-minute signal windows are computed. Design a fast check for the feature calculation and a separate gate that would catch degraded model quality before promotion.  
    Assess using a small fixture with known expected results at window boundaries, plus a model-quality comparison on a fixed evaluation set under a predefined tolerance. [Google Cloud — MLOps continuous delivery and automation pipelines](https://docs.cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning)

47. **Trace a bad model to its inputs** (Intermediate)  
    A production model version shows unexpectedly poor results at one station. Which lineage links should allow the team to trace from that deployed model back through the training run and processed dataset to its raw source, and why is each link useful?  
    Assess linking the deployed artifact to its training run, code and configuration, processed dataset and transformation version, and raw data snapshot. Explain how those links help reproduce results or locate the failure. [Google Cloud — AI and ML reliability](https://docs.cloud.google.com/architecture/framework/perspectives/ai-ml/reliability)

48. **When to trigger retraining** (Advanced)  
    Weekly labels reveal declining recall for crowded rooms, and retraining every week is expensive. How would you decide when to retrain, and what evidence should a newly trained model provide before promotion?  
    Assess setting an actionable recall threshold over comparable windows with enough labelled events, evaluating a candidate against the current model on the same data, and requiring a validation gate for promotion. [Google Cloud — MLOps continuous delivery and automation pipelines](https://docs.cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning)

49. **Secrets and least privilege** (Intermediate)  
    A training pipeline needs read access to one raw-data bucket and write access to the model registry. A notebook token currently has administrator access to the whole cloud project. Explain the least-privilege redesign and one operational benefit beyond security.  
    Assess using a dedicated service identity with read access to the bucket and write access to the registry, using managed credentials, and showing how a separate identity makes pipeline actions easier to trace. [Google Cloud — AI and ML security](https://docs.cloud.google.com/architecture/framework/perspectives/ai-ml/security)

50. **Evaluating a candidate on important slices** (Advanced)  
    A candidate model improves overall error by 2%, but its error worsens for large venues and older receivers. How should the team decide whether to promote it, and what artefacts should make that decision reviewable later?  
    Assess checking predeclared slices alongside the aggregate, using explicit acceptance criteria for important groups, and recording the dataset version, slice definitions, metrics and promotion decision. [Google Cloud — Model evaluation slices](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1/projects.locations.models.evaluations.slices), [MLflow — Model Registry](https://mlflow.org/docs/latest/ml/model-registry/)
