"""Verifier eval: synthetic concept notes with seeded errors.

Each case gives the sources (S1..), a note in the generator's output shape, and the seeded
errors by element id as the verifier sees them (claims C1.. in section order idea →
worked_example → why_it_works → common_mistakes, then D1 for the diagram; practice P1..).
Error kinds:
  unsupported  - the claim is false or not in the sources: must be ruled unsupported
  miscited     - true per a different source than the one cited: unsupported or citation_ok false
  wrong_answer - the practice answer is wrong: must be ruled answer_correct false
Everything not listed is correct and correctly cited; rejecting it is a false reject.
All text is written for this eval (no real course material).
"""

from __future__ import annotations

from typing import Any

Claim = tuple[str, list[str]]
Practice = tuple[str, str, list[str]]  # (prompt, answer, sources)


def case(
    cid: str,
    name: str,
    sources: list[tuple[str, str, str]],
    *,
    idea: list[Claim],
    worked: list[Claim] | None = None,
    why: list[Claim] | None = None,
    mistakes: list[Claim] | None = None,
    practice: list[Practice],
    errors: dict[str, str],
    diagram: str | None = None,
) -> dict[str, Any]:
    def claims(cs: list[Claim] | None) -> list[dict[str, Any]]:
        return [{"text": t, "sources": s} for t, s in cs or []]

    return {
        "id": cid,
        "sources": [
            {"id": f"S{n}", "title": title, "ref": ref, "text": text}
            for n, (title, ref, text) in enumerate(sources, start=1)
        ],
        "note": {
            "name": name,
            "idea": claims(idea),
            "diagram": diagram,
            "worked_example": claims(worked),
            "why_it_works": claims(why),
            "common_mistakes": claims(mistakes),
            "practice": [
                {"kind": "question", "prompt": p, "answer": a, "sources": s} for p, a, s in practice
            ],
            "related": [],
        },
        "errors": errors,
    }


CASES: list[dict[str, Any]] = [
    case(
        "v01",
        "Big-O notation",
        [
            (
                "Lecture 1 slides",
                "slide 4",
                "f(n) is O(g(n)) if there are constants c > 0 and n0 such that f(n) <= c*g(n) for all n >= n0.",
            ),
            (
                "Lecture 1 slides",
                "slide 5",
                "Big-O gives an upper bound only. For example, n is O(n^2), although the bound is not tight.",
            ),
            ("Lecture 1 slides", "slide 6", "Example: 4n + 10 is O(n), using c = 5 and n0 = 10."),
        ],
        idea=[("f(n) is O(g(n)) when f(n) <= c*g(n) for some c > 0 and every n >= n0.", ["S1"])],
        worked=[("4n + 10 is O(n) with c = 5 and n0 = 10.", ["S1"])],
        mistakes=[
            (
                "Big-O is only an upper bound: n is O(n^2) even though that bound is not tight.",
                ["S2"],
            )
        ],
        practice=[
            ("Is 4n + 10 in O(n)? Give witnesses.", "Yes: c = 5 and n0 = 10.", ["S3"]),
            ("Is n in O(n^2)?", "No, because the bound is not tight.", ["S2"]),
        ],
        errors={"C2": "miscited", "P2": "wrong_answer"},
    ),
    case(
        "v02",
        "Master theorem",
        [
            (
                "Lecture 4 slides",
                "slide 3",
                "The master theorem applies to T(n) = aT(n/b) + f(n) with a >= 1 and b > 1. Compare f(n) with n^(log_b a).",
            ),
            (
                "Lecture 4 slides",
                "slide 4",
                "Case 1: f(n) = O(n^(log_b a - e)) gives Theta(n^(log_b a)). Case 2: f(n) = Theta(n^(log_b a)) gives Theta(n^(log_b a) log n). Case 3: f(n) = Omega(n^(log_b a + e)) and regularity give Theta(f(n)).",
            ),
            (
                "Lecture 4 slides",
                "slide 5",
                "Example: T(n) = 2T(n/2) + n has n^(log_2 2) = n, so case 2 gives Theta(n log n).",
            ),
        ],
        idea=[("For T(n) = aT(n/b) + f(n), compare f(n) with n^(log_b a).", ["S1"])],
        worked=[
            ("T(n) = 2T(n/2) + n: n^(log_2 2) = n, so case 2 applies.", ["S3"]),
            ("Case 2 here gives Theta(n).", ["S3"]),
        ],
        why=[
            ("Case 3 needs the regularity condition in addition to the growth condition.", ["S2"])
        ],
        practice=[
            ("Solve T(n) = 2T(n/2) + n.", "Theta(n log n), by case 2.", ["S3"]),
            ("Which case covers f(n) = Theta(n^(log_b a))?", "Case 1.", ["S2"]),
        ],
        errors={"C3": "unsupported", "P2": "wrong_answer"},
    ),
    case(
        "v03",
        "Binary search",
        [
            (
                "Lecture 2 notes",
                "page 3",
                "Binary search finds a key in a sorted array by comparing with the middle element and discarding half of the remaining range each step.",
            ),
            (
                "Lecture 2 notes",
                "page 4",
                "Because the range halves each step, binary search makes O(log n) comparisons in the worst case. It requires the array to be sorted.",
            ),
        ],
        idea=[
            (
                "Binary search compares the key with the middle element and discards half the range each step.",
                ["S1"],
            )
        ],
        why=[("Halving the range each step gives O(log n) comparisons in the worst case.", ["S2"])],
        mistakes=[
            ("Binary search also works on unsorted arrays if you check both halves.", ["S2"])
        ],
        practice=[
            (
                "What is the worst-case number of comparisons for binary search on n elements?",
                "O(log n).",
                ["S2"],
            ),
            ("What must be true of the array before binary search?", "It must be sorted.", ["S2"]),
        ],
        errors={"C3": "unsupported"},
    ),
    case(
        "v04",
        "Merge sort",
        [
            (
                "Lecture 5 slides",
                "slide 2",
                "Merge sort splits the array in half, sorts each half recursively, and merges the two sorted halves.",
            ),
            (
                "Lecture 5 slides",
                "slide 3",
                "Merging two sorted lists of total length n takes Theta(n) time.",
            ),
            (
                "Lecture 5 slides",
                "slide 4",
                "The recurrence T(n) = 2T(n/2) + Theta(n) gives Theta(n log n) in every case, including the worst case.",
            ),
        ],
        idea=[("Merge sort splits in half, sorts each half recursively, and merges them.", ["S1"])],
        worked=[("Merging two sorted lists with n elements in total takes Theta(n).", ["S1"])],
        why=[("The recurrence 2T(n/2) + Theta(n) solves to Theta(n log n).", ["S3"])],
        practice=[
            (
                "What is merge sort's worst-case running time?",
                "Theta(n^2), when the input is reversed.",
                ["S3"],
            ),
            ("How long does merging take for n total elements?", "Theta(n).", ["S2"]),
        ],
        errors={"C2": "miscited", "P1": "wrong_answer"},
    ),
    case(
        "v05",
        "Binary heaps",
        [
            (
                "Lecture 9 slides",
                "slide 3",
                "A binary max-heap is a complete binary tree in which every node's key is at least the keys of its children.",
            ),
            (
                "Lecture 9 slides",
                "slide 6",
                "Insert and extract-max take O(log n) because they move an element along one root-to-leaf path.",
            ),
            (
                "Lecture 9 slides",
                "slide 8",
                "Building a heap bottom-up from n elements takes Theta(n) total time, because most nodes are near the bottom and sift down only a little.",
            ),
        ],
        idea=[
            (
                "In a max-heap every node's key is at least its children's keys, in a complete binary tree.",
                ["S1"],
            )
        ],
        worked=[
            ("Extract-max takes O(log n): the element moves along one root-to-leaf path.", ["S2"])
        ],
        why=[("Bottom-up heap construction is Theta(n log n) and that bound is tight.", ["S3"])],
        practice=[
            ("What is the cost of building a heap bottom-up?", "Theta(n).", ["S3"]),
            (
                "Why is insert O(log n)?",
                "The new element moves along a single root-to-leaf path, whose length is O(log n).",
                ["S2"],
            ),
        ],
        errors={"C3": "unsupported"},
    ),
    case(
        "v06",
        "Dijkstra's algorithm",
        [
            (
                "Lecture 15 notes",
                "page 2",
                "Dijkstra's algorithm computes shortest paths from one source when all edge weights are non-negative.",
            ),
            (
                "Lecture 15 notes",
                "page 3",
                "It repeatedly removes the unvisited vertex with the smallest tentative distance and relaxes its outgoing edges.",
            ),
            (
                "Lecture 15 notes",
                "page 5",
                "With negative edge weights Dijkstra can return wrong distances; use Bellman-Ford instead.",
            ),
        ],
        idea=[
            (
                "Dijkstra's algorithm finds single-source shortest paths when every edge weight is non-negative.",
                ["S1"],
            )
        ],
        worked=[
            (
                "Each step removes the closest unvisited vertex and relaxes its outgoing edges.",
                ["S2"],
            )
        ],
        mistakes=[
            ("Dijkstra handles negative edges as long as there is no negative cycle.", ["S3"])
        ],
        practice=[
            (
                "What should you use instead of Dijkstra when some edges are negative?",
                "Bellman-Ford.",
                ["S3"],
            ),
            (
                "Which vertex does Dijkstra pick next?",
                "The unvisited vertex with the largest tentative distance.",
                ["S2"],
            ),
        ],
        errors={"C3": "unsupported", "P2": "wrong_answer"},
    ),
    case(
        "v07",
        "Eigenvalues",
        [
            (
                "Textbook 5.1",
                "page 268",
                "A scalar lambda is an eigenvalue of A if Av = lambda v for some nonzero vector v.",
            ),
            (
                "Textbook 5.2",
                "page 276",
                "The eigenvalues of A are the solutions of det(A - lambda I) = 0, the characteristic equation.",
            ),
            (
                "Textbook 5.2",
                "page 277",
                "The eigenvalues of a triangular matrix are the entries on its main diagonal.",
            ),
        ],
        idea=[("lambda is an eigenvalue of A when Av = lambda v for some nonzero v.", ["S1"])],
        worked=[("Solve det(A - lambda I) = 0 to find the eigenvalues.", ["S2"])],
        why=[("For a triangular matrix, the eigenvalues are the diagonal entries.", ["S3"])],
        practice=[
            (
                "What are the eigenvalues of the upper triangular matrix [[2, 5], [0, 3]]?",
                "2 and 3.",
                ["S3"],
            ),
            ("What are the eigenvalues of [[4, 0], [0, -1]]?", "4 and 1.", ["S3"]),
        ],
        errors={"P2": "wrong_answer"},
    ),
    case(
        "v08",
        "Rank of a matrix",
        [
            ("Textbook 4.6", "page 232", "The rank of A is the dimension of its column space."),
            (
                "Textbook 4.6",
                "page 233",
                "Rank theorem: for an m x n matrix, rank A + dim Nul A = n.",
            ),
            (
                "Textbook 4.6",
                "page 234",
                "The pivot columns of A form a basis for Col A, so the rank equals the number of pivot columns.",
            ),
        ],
        idea=[("The rank of A is the dimension of its column space.", ["S1"])],
        worked=[("Count the pivot columns to get the rank.", ["S1"])],
        why=[("For an m x n matrix, rank A + dim Nul A = n.", ["S2"])],
        practice=[
            ("A 4 x 6 matrix has rank 4. What is dim Nul A?", "2.", ["S2"]),
            ("A 3 x 5 matrix has 3 pivot columns. What is its rank?", "3.", ["S3"]),
        ],
        errors={"C2": "miscited"},
    ),
    case(
        "v09",
        "Linear independence",
        [
            (
                "Textbook 1.7",
                "page 56",
                "Vectors v1..vp are linearly independent if the only solution of x1 v1 + ... + xp vp = 0 is the trivial solution.",
            ),
            (
                "Textbook 1.7",
                "page 58",
                "Any set of more than n vectors in R^n is linearly dependent.",
            ),
            ("Textbook 1.7", "page 59", "A set containing the zero vector is linearly dependent."),
        ],
        idea=[
            (
                "Vectors are linearly independent when only the trivial combination gives the zero vector.",
                ["S1"],
            )
        ],
        why=[("More than n vectors in R^n are always dependent.", ["S2"])],
        mistakes=[
            (
                "A set that contains the zero vector can still be independent if the other vectors are.",
                ["S3"],
            ),
            ("Four vectors in R^3 can be linearly independent if no two are parallel.", ["S2"]),
        ],
        practice=[
            (
                "Are any 5 vectors in R^4 linearly independent?",
                "No, more than 4 vectors in R^4 are always dependent.",
                ["S2"],
            ),
        ],
        errors={"C3": "unsupported", "C4": "unsupported"},
    ),
    case(
        "v10",
        "The chain rule",
        [
            (
                "Calculus notes",
                "page 14",
                "Chain rule: if y = f(g(x)), then dy/dx = f'(g(x)) * g'(x).",
            ),
            ("Calculus notes", "page 15", "Example: d/dx sin(x^2) = cos(x^2) * 2x."),
        ],
        idea=[("For y = f(g(x)), dy/dx = f'(g(x)) * g'(x).", ["S1"])],
        worked=[("d/dx sin(x^2) = cos(x^2) * 2x.", ["S2"])],
        mistakes=[("Forgetting the inner derivative g'(x) is the usual error.", ["S1"])],
        practice=[
            ("Differentiate sin(x^2).", "cos(x^2) * 2x.", ["S2"]),
            ("Differentiate (3x + 1)^2 using the chain rule.", "2(3x + 1).", ["S1"]),
        ],
        errors={"P2": "wrong_answer"},
    ),
    case(
        "v11",
        "Photosynthesis",
        [
            (
                "Biology lecture 8",
                "slide 4",
                "Photosynthesis converts light energy into chemical energy, producing glucose from carbon dioxide and water.",
            ),
            (
                "Biology lecture 8",
                "slide 6",
                "The oxygen released in photosynthesis comes from splitting water molecules in the light reactions.",
            ),
            (
                "Biology lecture 8",
                "slide 9",
                "The Calvin cycle fixes carbon dioxide into sugar using ATP and NADPH from the light reactions.",
            ),
        ],
        idea=[
            (
                "Photosynthesis turns light energy into chemical energy, making glucose from CO2 and water.",
                ["S1"],
            )
        ],
        worked=[("The Calvin cycle uses ATP and NADPH to fix CO2 into sugar.", ["S3"])],
        why=[("The released oxygen comes from the carbon dioxide molecules.", ["S2"])],
        practice=[
            (
                "Where does the oxygen released by photosynthesis come from?",
                "From water split in the light reactions.",
                ["S2"],
            ),
            ("What does the Calvin cycle use from the light reactions?", "ATP and NADPH.", ["S3"]),
        ],
        errors={"C3": "unsupported"},
    ),
    case(
        "v12",
        "Supply and demand",
        [
            (
                "Econ lecture 3",
                "slide 5",
                "The law of demand: other things equal, when price rises, quantity demanded falls.",
            ),
            (
                "Econ lecture 3",
                "slide 8",
                "An increase in demand shifts the demand curve right, raising both equilibrium price and quantity.",
            ),
            (
                "Econ lecture 3",
                "slide 11",
                "A price ceiling set below equilibrium causes a shortage.",
            ),
        ],
        idea=[("When price rises, quantity demanded falls, other things equal.", ["S1"])],
        worked=[("An increase in demand raises equilibrium price and quantity.", ["S2"])],
        mistakes=[("A price ceiling below equilibrium causes a surplus.", ["S3"])],
        practice=[
            ("What happens to equilibrium price when demand increases?", "It falls.", ["S2"]),
            ("What does a binding price ceiling cause?", "A shortage.", ["S3"]),
        ],
        errors={"C3": "unsupported", "P1": "wrong_answer"},
    ),
    case(
        "v13",
        "Newton's second law",
        [
            (
                "Physics lecture 4",
                "slide 2",
                "Newton's second law: the net force on an object equals its mass times its acceleration, F = ma.",
            ),
            ("Physics lecture 4", "slide 3", "Force is measured in newtons; 1 N = 1 kg m/s^2."),
            (
                "Physics lecture 4",
                "slide 7",
                "Example: a net force of 10 N on a 2 kg cart gives an acceleration of 5 m/s^2.",
            ),
        ],
        idea=[("Net force equals mass times acceleration, F = ma.", ["S1"])],
        worked=[("10 N on a 2 kg cart gives 5 m/s^2.", ["S3"]), ("1 N is 1 kg m/s^2.", ["S1"])],
        practice=[
            ("What acceleration does 12 N give a 3 kg mass?", "4 m/s^2.", ["S1"]),
            ("What is 1 newton in base units?", "1 kg m/s.", ["S2"]),
        ],
        errors={"C3": "miscited", "P2": "wrong_answer"},
    ),
    case(
        "v14",
        "Thesis statements",
        [
            (
                "Writing guide",
                "page 3",
                "A thesis states the essay's main claim in one or two sentences, and it must be arguable: a reasonable reader could disagree.",
            ),
            (
                "Writing guide",
                "page 4",
                "A statement of fact is not a thesis, because there is nothing to argue.",
            ),
        ],
        idea=[
            (
                "A thesis states the main claim in a sentence or two, and it must be arguable.",
                ["S1"],
            )
        ],
        mistakes=[
            ("A statement of fact is not a thesis: nothing is left to argue.", ["S2"]),
            ("The best thesis is a neutral summary that avoids taking a side.", ["S1"]),
        ],
        practice=[
            (
                "Is 'The novel was published in 1925' a thesis?",
                "No, it is a statement of fact, so there is nothing to argue.",
                ["S2"],
            ),
        ],
        errors={"C3": "unsupported"},
    ),
    case(
        "v15",
        "Hash tables",
        [
            (
                "Lecture 11 slides",
                "slide 4",
                "With a good hash function and load factor kept constant, hash table lookup takes O(1) expected time.",
            ),
            (
                "Lecture 11 slides",
                "slide 6",
                "In the worst case, when all keys collide, a lookup with chaining takes O(n) time.",
            ),
            (
                "Lecture 11 slides",
                "slide 9",
                "The load factor is the number of stored keys divided by the number of buckets.",
            ),
        ],
        idea=[
            (
                "Lookup takes O(1) expected time with a good hash function and a constant load factor.",
                ["S1"],
            )
        ],
        worked=[("Load factor = keys stored / number of buckets.", ["S3"])],
        mistakes=[("Hash table lookup is O(1) even in the worst case.", ["S2"])],
        practice=[
            (
                "What is the worst-case lookup time with chaining?",
                "O(n), when every key lands in one bucket.",
                ["S2"],
            ),
            ("A table has 50 keys in 100 buckets. What is the load factor?", "2.", ["S3"]),
        ],
        errors={"C3": "unsupported", "P2": "wrong_answer"},
    ),
    case(
        "v16",
        "Recursion trees",
        [
            (
                "Lecture 3 slides",
                "slide 7",
                "A recursion tree draws each recursive call as a node labeled with the non-recursive work it does.",
            ),
            (
                "Lecture 3 slides",
                "slide 8",
                "For T(n) = 2T(n/2) + n, each level of the tree does n work in total and there are log n levels.",
            ),
        ],
        idea=[
            (
                "A recursion tree draws each call as a node labeled with its non-recursive work.",
                ["S1"],
            )
        ],
        worked=[("For 2T(n/2) + n, each level totals n and there are log n levels.", ["S2"])],
        practice=[
            ("How many levels does the recursion tree of 2T(n/2) + n have?", "About log n.", ["S2"])
        ],
        diagram="graph TD\n  A[n] --> B[n/2]\n  A --> C[n/2]\n  B --> D[n/4]\n  C --> E[n/4]\n  D --> F[each level totals n^2]",
        errors={"D1": "unsupported"},
    ),
    case(
        "v17",
        "Memoization",
        [
            (
                "Lecture 13 slides",
                "slide 2",
                "Memoization stores the result of each subproblem the first time it is computed and returns the stored result afterwards.",
            ),
            (
                "Lecture 13 slides",
                "slide 5",
                "Memoized Fibonacci computes each fib(i) for i from 0 to n once, so it makes O(n) calls that do real work.",
            ),
            (
                "Lecture 13 slides",
                "slide 6",
                "Naive recursive Fibonacci takes exponential time because it recomputes the same subproblems.",
            ),
        ],
        idea=[
            ("Memoization stores each subproblem's result the first time and reuses it.", ["S1"])
        ],
        why=[("Naive Fibonacci is exponential because it recomputes subproblems.", ["S3"])],
        practice=[
            ("How many calls doing real work does memoized fib(n) make?", "O(n).", ["S2"]),
            ("Why is naive recursive Fibonacci slow?", "Because each call does O(n) work.", ["S3"]),
        ],
        errors={"P2": "wrong_answer"},
    ),
    case(
        "v18",
        "Bayes' rule",
        [
            ("Stats lecture 6", "slide 3", "Bayes' rule: P(A|B) = P(B|A) P(A) / P(B)."),
            (
                "Stats lecture 6",
                "slide 5",
                "P(B) can be expanded with the law of total probability: P(B) = P(B|A)P(A) + P(B|not A)P(not A).",
            ),
            (
                "Stats lecture 6",
                "slide 7",
                "Example: with P(A) = 0.5, P(B|A) = 0.8, and P(B) = 0.5, P(A|B) = 0.8.",
            ),
        ],
        idea=[("P(A|B) = P(B|A) P(A) / P(B).", ["S1"])],
        worked=[("Expand P(B) as P(B|A)P(A) + P(B|not A)P(not A).", ["S1"])],
        practice=[
            ("P(A) = 0.5, P(B|A) = 0.8, P(B) = 0.5. What is P(A|B)?", "0.8.", ["S3"]),
            ("P(A) = 0.2, P(B|A) = 0.5, P(B) = 0.25. What is P(A|B)?", "0.1.", ["S1"]),
        ],
        errors={"C2": "miscited", "P2": "wrong_answer"},
    ),
    case(
        "v19",
        "The normal distribution",
        [
            (
                "Stats lecture 4",
                "slide 10",
                "For a normal distribution, about 68% of values lie within one standard deviation of the mean, about 95% within two, and about 99.7% within three.",
            ),
            ("Stats lecture 4", "slide 11", "A z-score is (x - mean) / standard deviation."),
        ],
        idea=[("About 68% of values lie within one standard deviation of the mean.", ["S1"])],
        worked=[
            ("A z-score is (x - mean) / sd.", ["S2"]),
            ("About 90% lie within two standard deviations.", ["S1"]),
        ],
        practice=[
            ("Mean 70, sd 5. What is the z-score of 80?", "2.", ["S2"]),
            ("About what fraction lies within three standard deviations?", "About 99.7%.", ["S1"]),
        ],
        errors={"C3": "unsupported"},
    ),
    case(
        "v20",
        "Breadth-first search",
        [
            (
                "Lecture 14 notes",
                "page 1",
                "Breadth-first search explores vertices in order of their distance in edges from the start, using a FIFO queue.",
            ),
            (
                "Lecture 14 notes",
                "page 2",
                "In an unweighted graph, BFS finds shortest paths measured in number of edges.",
            ),
            ("Lecture 14 notes", "page 3", "BFS runs in O(V + E) time with an adjacency list."),
        ],
        idea=[
            (
                "BFS visits vertices in order of edge distance from the start, using a FIFO queue.",
                ["S1"],
            )
        ],
        worked=[("With adjacency lists BFS takes O(V + E).", ["S3"])],
        why=[("In unweighted graphs BFS gives shortest paths by edge count.", ["S2"])],
        practice=[
            ("What data structure does BFS use for the frontier?", "A stack.", ["S1"]),
            ("What is BFS's running time with adjacency lists?", "O(V + E).", ["S3"]),
        ],
        errors={"P1": "wrong_answer"},
    ),
    case(
        "v21",
        "Big-Theta",
        [
            (
                "Lecture 1 slides",
                "slide 9",
                "f(n) is Theta(g(n)) when f(n) is both O(g(n)) and Omega(g(n)).",
            ),
            ("Lecture 1 slides", "slide 10", "Example: 3n^2 + n is Theta(n^2)."),
        ],
        idea=[("f is Theta(g) when f is both O(g) and Omega(g).", ["S1"])],
        worked=[("3n^2 + n is Theta(n^2).", ["S2"])],
        practice=[("Is 3n^2 + n in Theta(n^2)?", "Yes.", ["S2"])],
        diagram="flowchart LR\n  A[f is O of g] --> C[f is Theta of g]\n  B[f is Omega of g] -.-> D[not needed]",
        errors={"D1": "unsupported"},
    ),
    case(
        "v22",
        "Topic sentences",
        [
            (
                "Writing guide",
                "page 7",
                "A topic sentence opens a body paragraph and states the one point the paragraph supports.",
            ),
            (
                "Writing guide",
                "page 8",
                "Each sentence in the paragraph should support the topic sentence; move or cut sentences that do not.",
            ),
        ],
        idea=[
            ("A topic sentence opens a paragraph and states the single point it supports.", ["S1"])
        ],
        mistakes=[
            ("Every sentence should support the topic sentence; cut or move the rest.", ["S1"])
        ],
        practice=[
            (
                "Where does a topic sentence usually go?",
                "At the start of the body paragraph.",
                ["S1"],
            ),
            (
                "What should you do with a sentence that doesn't support the topic sentence?",
                "Keep it, since it adds variety.",
                ["S2"],
            ),
        ],
        errors={"C2": "miscited", "P2": "wrong_answer"},
    ),
]
