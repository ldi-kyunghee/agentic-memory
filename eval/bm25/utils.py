import yaml
from pydantic import BaseModel, Field
from typing_extensions import Literal

PROMPT = """
   You are an intelligent memory assistant tasked with retrieving accurate information from conversation memories.

   # CONTEXT:
   You have access to memories from two speakers in a conversation. These memories contain 
   timestamped information that may be relevant to answering the question.

   # INSTRUCTIONS:
   1. Carefully analyze all provided memories from both speakers
   2. Pay special attention to the timestamps to determine the answer
   3. If the question asks about a specific event or fact, look for direct evidence in the memories
   4. If the memories contain contradictory information, prioritize the most recent memory
   5. If there is a question about time references (like "last year", "two months ago", etc.), calculate the actual date based on the memory timestamp. For example, if a memory from 4 May 2022 mentions "went to India last year," then the trip occurred in 2021.
   6. Always convert relative time references to specific dates, months, or years. For example, convert "last year" to "2022" or "two months ago" to "March 2023" based on the memory timestamp. Ignore the reference while answering the question.
   7. Focus only on the content of the memories from both speakers. Do not confuse character names mentioned in memories with the actual users who created those memories.
   8. The answer should be less than 5-6 words.

   # APPROACH (Think step by step):
   1. First, examine all memories that contain information related to the question
   2. Examine the timestamps and content of these memories carefully
   3. Look for explicit mentions of dates, times, locations, or events that answer the question
   4. If the answer requires calculation (e.g., converting relative time references), show your work
   5. Formulate a precise, concise answer based solely on the evidence in the memories
   6. Double-check that your answer directly addresses the question asked
   7. Ensure your final answer is specific and avoids vague time references

   {context}

   Question: {question}

   Answer:
   """

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

EVALUATION_PROMPT_FOR_QA = """You are an **evaluation expert for AI memory system question answering**.
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

"""

class QAEval(BaseModel):
  reasoning: str = Field(description="Reasoning content: Provide a concise and traceable evaluation rationale: first compare the system’s response with the Key Memory Points (which were correctly used, which were missing, and whether there was any fabrication/contradiction), then assess its consistency with the Reference Answer, and finally state the classification basis.")
  evaluation_result: Literal["Correct", "Hallucination", "Omission"]

EVALUATION_PROMPT_FOR_QA += f"""
# Output Requirements

Your output, including your reasoning, must adhere to the following JSON schema:

{QAEval.model_json_schema()}
"""
  
mem_template = """[{}]
        User: {}
        Assistant: {}"""

mem_template_with_prev = """[{}]
        Assistant: {}
        User: {}
        Assistant: {}"""

def load_config(config_file):
    with open(f"configs/bm25_eval/{config_file}", "r") as file:
        model_kwargs = yaml.safe_load(file)

    if model_kwargs.get('sampling_params'):
      sampling_params = model_kwargs.pop("sampling_params")
      return model_kwargs, sampling_params
    
    if model_kwargs.get('generation_args'):
      generation_args = model_kwargs.pop('generation_args')
      sampling_params = generation_args.pop('sampling_params')
      return model_kwargs, sampling_params, generation_args
    return model_kwargs

def add_memory_from_dialogue(session_dialogue):
    user_dialogue = session_dialogue[::2]
    assistant_dialogue = session_dialogue[1::2]

    per_session_memories = []
    for user, assistant in zip(user_dialogue, assistant_dialogue):
        memory = mem_template.format(
            user['timestamp'],
            user['content'],
            assistant['content']
        )

        per_session_memories.append(memory)
    
    return per_session_memories

def add_memory_from_dialogue_with_prev(session_dialogue):
    user_dialogue = session_dialogue[::2]
    assistant_dialogue = session_dialogue[1::2]

    per_session_memories = []
    question = ""
    for i, (user, assistant) in enumerate(zip(user_dialogue, assistant_dialogue)):
        if question:
            memory = mem_template_with_prev.format(
              user['timestamp'],
              question,
              user['content'],
              assistant['content']
            )

        else:
            memory = mem_template.format(
                user['timestamp'],
                user['content'],
                assistant['content']
            )

        if assistant['content'].strip().endswith('?'):
            question = assistant['content'].split('.')[-1]
        else:
            question = ""

        per_session_memories.append(memory)
    
    return per_session_memories

def per_persona_dataset(persona, memory_with_prior_question: bool):
    sessions = [session for session in persona['sessions'] if session.get('questions')]
    dialogue = [session['dialogue'] for session in sessions]
    qa_data = []
    for session in sessions:
      qa_data += session['questions']

    add_memory = add_memory_from_dialogue_with_prev if memory_with_prior_question else add_memory_from_dialogue

    memories = []
    for per_session_dialogue in dialogue:
      memories += add_memory(per_session_dialogue)

    return qa_data, memories
