---
license: cc-by-nc-nd-4.0
task_categories:
- question-answering
language:
- en
tags:
- memory
- memory system
- hallucination
- benchmark
- evaluation
- LLM
- long-term memory
pretty_name: HaluMem
size_categories:
- 1K<n<10K
source_datasets:
- original
---

<h1 align="center">
    HaluMem: A Comprehensive Benchmark for Evaluating Hallucinations in Memory Systems
</h1>
<p align="center">
<a href="https://spdx.org/licenses/CC-BY-NC-ND-4.0.html">
    <img alt="License: CC-BY-NC-ND-4.0" src="https://img.shields.io/badge/License-CC_BY_NC_ND_4.0-brightgreen.svg">
</a>
<a href="https://github.com/MemTensor/HaluMem">
  <img alt="GitHub stars" src="https://img.shields.io/github/stars/MemTensor/HaluMem?color=blueviolet&style=flat-square&logo=github">
</a>
<a href="https://arxiv.org/abs/2511.03506">
     <img src="https://img.shields.io/badge/arXiv-Paper-B31B1B?style=flat-square&logo=arxiv&logoColor=white">
</a>
</p>

## 📊 Why We Define the HaluMem Evaluation Tasks

- **Limitations of Existing Frameworks**
   Most existing evaluation frameworks treat memory systems as **black-box models**, assessing performance only through **end-to-end QA accuracy**.
   However, this approach has **two major limitations**:
  1. It lacks a **hallucination evaluation** specifically designed for the characteristics of memory systems.
  2. It fails to examine the **core operational steps** in how memory is processed, such as retrieval and updating.
- **Motivation for HaluMem**
   To address these issues, we introduce **HaluMem**, a comprehensive benchmark that defines fine-grained evaluation tasks tailored for memory systems.

------

## 🧩 What Is HaluMem?

The paper *“HaluMem: A Comprehensive Benchmark for Evaluating Hallucinations in Memory Systems”* presents the **first operation-level hallucination benchmark** designed explicitly for memory systems.

HaluMem decomposes the memory workflow into **three fundamental operations**:

- **🧩 Memory Extraction**
   Evaluates whether the system can **accurately identify and store factual information** from dialogue sessions while avoiding **hallucinated or irrelevant memories**.
   This task measures both **memory completeness** (how well the reference memory points are captured) and **memory accuracy** (how precisely the extracted memories reflect the ground truth).
- **🔄 Memory Update**
   Evaluates whether the system can **correctly modify or overwrite existing memories** when new dialogue provides updated or contradictory information, ensuring internal **consistency and temporal coherence** within the memory base.
- **💬 Memory Question Answering**
   Evaluates the system’s **end-to-end capability** to integrate multiple memory processes  (including **extraction**, **update**, **retrieval**, and **response generation**) to produce factual, context-aware, and hallucination-free answers.

Each operation includes carefully designed evaluation tasks to **reveal hallucination behaviors** at different stages of memory handling.

------

## 📚 Dataset Overview

HaluMem consists of two dataset versions:

| Dataset         | #Users | #Dialogues | Avg. Sessions/User | Avg. Context Length | #Memory Points | #QA Pairs |
| --------------- | ------ | ---------- | ------------------ | ------------------- | -------------- | --------- |
| **Halu-Medium** | 20     | 30,073     | 70                 | ~160k tokens        | 14,948         | 3,467     |
| **Halu-Long**   | 20     | 53,516     | 120                | ~1M tokens          | 14,948         | 3,467     |

- **Halu-Medium** provides multi-turn human-AI dialogue sessions for evaluating memory hallucinations in standard-length contexts.
- **Halu-Long** extends context length to **1M tokens** per user, introducing large-scale **interference and distractor content** (e.g., factual QA and math problems) to assess robustness and hallucination resistance.

------

## 🧱 Dataset Structure

Each user’s data is stored as a **JSON object** containing:

| Field          | Description                                                  |
| -------------- | ------------------------------------------------------------ |
| `uuid`         | Unique user identifier                                       |
| `persona_info` | Persona profile including background, traits, goals, and motivations |
| `sessions`     | List of multi-turn conversational sessions                   |

Each `session` includes:

| Field                    | Description                                           |
| ------------------------ | ----------------------------------------------------- |
| `start_time`, `end_time` | Session timestamps                                    |
| `dialogue_turn_num`      | Total turns in the dialogue                           |
| `dialogue`               | Sequence of utterances between `user` and `assistant` |
| `memory_points`          | List of extracted memory elements from the session    |
| `questions`              | QA pairs used for memory reasoning and evaluation     |
| `dialogue_token_length`  | Tokenized length of the full dialogue                 |

#### Memory Point Structure

Each memory point captures a **specific fact or event** derived from dialogue.

| Field               | Description                                                  |
| ------------------- | ------------------------------------------------------------ |
| `index`             | Memory ID within the session                                 |
| `memory_content`    | Text description of the memory                               |
| `memory_type`       | Type (e.g., *Persona Memory*, *Event Memory*, *Relationship Memory*) |
| `memory_source`     | Origin: `primary`, `secondary`, `interference`, or `system`  |
| `is_update`         | Indicates if it modifies an existing memory                  |
| `original_memories` | Previous related memories (if updated)                       |
| `importance`        | Relative salience score (0–1)                                |
| `timestamp`         | Time of creation or update                                   |

#### Memory Point Example

```json
{
    "index": 1,
    "memory_content": "Martin Mark is considering a career change due to the impact of his current role on his mental health.",
    "memory_type": "Event Memory",
    "memory_source": "secondary",
    "is_update": "True",
    "original_memories": [
        "Martin Mark is considering a career change due to health impacts from his current role."
    ],
    "timestamp": "Dec 15, 2025, 08:41:23",
    "importance": 0.75
}
```

#### Dialogue Structure

Each dialogue turn includes:

```json
[
    {
        "role": "user",
        "content": "I've been reflecting a lot on my career lately, especially how my current role as a director at Huaxin Consulting is impacting my mental health. It's becoming increasingly clear that I need to make a change.",
        "timestamp": "Dec 15, 2025, 06:11:23",
        "dialogue_turn": 0
    },
    {
        "role": "assistant",
        "content": "It's great that you're taking the time to reflect on your career, Martin. Recognizing the impact on your mental health is a crucial step. Balancing professional responsibilities with health is essential, especially given your commitment to improving healthcare access globally. Have you considered how a career change might not only address your health concerns but also align with your humanitarian goals and personal well-being?",
        "timestamp": "Dec 15, 2025, 06:11:23",
        "dialogue_turn": 0
    },
    ...
]
```

#### Question–Answer Structure

Each question tests **memory retrieval**, **reasoning**, or **hallucination control**:

```json
{
  "question": "What type of new physical activity might Martin be interested in trying after April 10, 2026?",
  "answer": "Other extreme sports.",
  "evidence": [
    {
      "memory_content": "Martin has developed a newfound appreciation for extreme sports...",
      "memory_type": "Persona Memory"
    }
  ],
  "difficulty": "medium",
  "question_type": "Generalization & Application"
}
```

------

## 🧬 Dataset Construction Process

The **HaluMem dataset** was built through a **six-stage, carefully controlled pipeline** that combines **programmatic generation**, **LLM-assisted refinement**, and **human validation** to ensure realism, consistency, and reliability.

1. **🧑‍💼 Stage 1: Persona Construction**
    Each dataset user begins with a richly detailed **virtual persona** consisting of three layers — *core profile information* (e.g., demographics, education, goals), *dynamic state information* (e.g., occupation, health, relationships), and *preference information* (e.g., food, music, hobbies).
    Personas were initially generated via rule-based templates seeded from **Persona Hub (1B+ personas)** and then refined using **GPT-4o**, ensuring logical coherence and natural diversity.
2. **📈 Stage 2: Life Skeleton Planning**
    A structured **life skeleton** defines each user’s evolving timeline, linking major career milestones and life events to the progression of dynamic and preference states.
    Controlled probabilistic mechanisms ensure realistic variation and coherent event evolution, forming a narrative blueprint for downstream data generation.
3. **📜 Stage 3: Event Flow Generation**
    The abstract life skeleton is converted into a **chronological event flow**, including:
   - **Init Events** — derived from initial persona profiles
   - **Career Events** — multi-stage professional or health-related developments
   - **Daily Events** — lifestyle or preference changes
      Together, these events form each user’s **memory timeline**, providing a consistent and interpretable narrative structure.
4. **🧠 Stage 4: Session Summaries & Memory Points**
    Each event is transformed into a **session summary** simulating a human–AI interaction. From these summaries, **structured memory points** are extracted, categorized into *Persona*, *Event*, and *Relationship* memories.
    Update-type memories maintain traceability by linking to their replaced versions, ensuring temporal consistency.
5. **💬 Stage 5: Multi-turn Session Generation**
    The summaries are expanded into **full dialogues** containing **adversarial distractor memories** — subtly incorrect facts introduced by the AI to simulate hallucination challenges.
    Additional irrelevant QAs are inserted to increase contextual complexity without altering original memories, mimicking real-world long-context noise.
6. **❓ Stage 6: Question Generation**
    Based on the sessions and memory points, **six types of evaluation questions** are automatically generated, covering both factual recall and reasoning tasks.
    Each question includes difficulty level, reasoning type, and direct evidence links to the supporting memory points.
7. **🧾 Human Annotation & Quality Verification**
    A team of 8 annotators manually reviewed over **50% of Halu-Medium**, scoring each session’s memory points and QA pairs on **correctness**, **relevance**, and **consistency**.
    Results demonstrate high data quality:
   - ✅ **Accuracy:** 95.70%
   - 📎 **Relevance:** 9.58 / 10
   - 🔁 **Consistency:** 9.45 / 10

------

> [!NOTE]
>
> 🧩 **In Summary:**
>  HaluMem provides a **comprehensive and standardized benchmark** for investigating hallucinations in memory systems.
>  By covering **core memory operations**, scaling **context length**, and introducing **distractor interference**, it establishes a robust foundation for **systematic hallucination research** in large language model memory architectures.

------


## Citation

```
@misc{chen2025halumemevaluatinghallucinationsmemory,
      title={HaluMem: Evaluating Hallucinations in Memory Systems of Agents}, 
      author={Ding Chen and Simin Niu and Kehang Li and Peng Liu and Xiangping Zheng and Bo Tang and Xinchi Li and Feiyu Xiong and Zhiyu Li},
      year={2025},
      eprint={2511.03506},
      archivePrefix={arXiv},
      primaryClass={cs.CL},
      url={https://arxiv.org/abs/2511.03506}, 
}
```