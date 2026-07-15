import yaml

PROMPT = (
    "Answer the following question based on the documents provided."
    "Documents: "
    "{documents}"
    "Question: {question}"
    "If the answer could not be found in the provided documents, do not hallucinate an answer."
    "Keep your answers short and brief, with no commentary."
).strip()

EVALUATION_PROMPT_FOR_QUESTION = """You are an **evaluation expert for AI memory system question answering**.
Based **only** on the provided **“Question”**, **“Reference Answer”**, and **“Key Memory Points”** (the essential facts needed to derive the reference answer), strictly evaluate the **accuracy** of the **“Memory System Response.”** Classify it as one of **“Correct”**, **“Hallucination”**, or **“Omission.”** Do **not** use any external knowledge or subjective inference. Finally, output your judgment **strictly** in the specified JSON format.

# Evaluation Criteria

## Answer Type Classification

### 1. Correct

* The “Memory System Response” accurately answers the “Question,” and its content is **semantically equivalent** to the “Reference Answer.”
* It contains **no contradictions** with the “Key Memory Points” or “Reference Answer.”
* It introduces **no unsupported details** beyond the “Key Memory Points” that could alter the conclusion.
* Synonyms, paraphrasing, and reasonable summarization are acceptable.

### 2. Hallucination

* The “Memory System Response” includes information or facts that **contradict or are inconsistent** with the “Reference Answer” or the “Key Memory Points.”
* When the “Reference Answer” is labeled as *unknown/uncertain*, yet the response provides a specific verifiable fact or conclusion.
* Extra irrelevant information that does **not change** the conclusion is **not** considered hallucination by itself; however, if it **changes or misleads** the conclusion, or **contradicts** the “Key Memory Points,” it should be judged as a **Hallucination**.

### 3. Omission

* The response is **incomplete** compared to the “Reference Answer.”
* It explicitly states “don’t know,” “can’t remember,” or “no related memory,” even though relevant information exists in the “Key Memory Points.”
* For multi-element questions, **all elements must be correct and present**; omission of **any** element is considered an **Omission**.

## Priority Rules (Conflict Handling)

* If the response contains **both missing necessary information** and **fabricated/contradictory information**, classify it as **Hallucination**.
* If there is **no fabrication/contradiction** but some necessary information is missing, classify it as **Omission**.
* Only when the meaning is **fully equivalent** to the reference answer should it be classified as **Correct**.

## Detailed Guidelines and Tolerance

* Equivalent expressions of numbers, times, and units are acceptable, but the **numerical values themselves must not differ**.
* For multi-element questions, **all elements must be complete and accurate**; missing any element counts as **Omission**.
* If the reference answer is *“unknown / cannot be determined”* and the system provides a definite fact, that is a **Hallucination**.
  If the system also answers *“unknown”* (without guessing), it may be **Correct**.
* The evaluation must rely **only** on the *Reference Answer*, *Key Memory Points*, and *System Response* — no external context, world knowledge, or speculative reasoning is allowed.

# Information for Evaluation

* **Question:**
  {question}

* **Reference Answer:**
  {reference_answer}

* **Key Memory Points:**
  {key_memory_points}

* **Memory System Response:**
  {response}

# Output Requirements

Please provide your evaluation result **strictly** in the JSON format below.
Do **not** add any extra explanation or comments outside the JSON block.

```json
{{
  "reasoning": "Provide a concise and traceable evaluation rationale: first compare the system’s response with the Key Memory Points (which were correctly used, which were missing, and whether there was any fabrication/contradiction), then assess its consistency with the Reference Answer, and finally state the classification basis.",
  "evaluation_result": "Correct | Hallucination | Omission"
}}
```
"""

def load_config(config_file):
    with open(f"configs/{config_file}", "r") as file:
        model_kwargs = yaml.safe_load(file)
    
    sampling_params = model_kwargs.pop("sampling_params")
    return model_kwargs, sampling_params

def add_memory_from_dialogue(session_dialogue):
    user_dialogue = session_dialogue[::2]
    assistant_dialogue = session_dialogue[1::2]

    for user, assistant in zip(user_dialogue):
        pass