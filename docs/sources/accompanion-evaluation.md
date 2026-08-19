# Source Detail: ACCompanion Evaluation

## Source

- Parent source note: [ACCompanion](accompanion.md)
- Paper: https://www.ijcai.org/proceedings/2023/0641.pdf

## Score-Follower Results

On selected complex solo piano performances:

- HMM median absolute asynchrony: 645.7 ms.
- OLTW median absolute asynchrony: 60.6 ms.
- OLTW had 86.7% of asynchronies within 100 ms.
- HMM had 5.5% within 100 ms.

These results support testing OLTW-style followers early for Rubato's Chopin
excerpt.

## Tempo-Model Results

On tempo/onset prediction:

- Reactive and moving-average models had onset errors above 4 seconds.
- Linear synchronization reduced onset error to 81.9 ms.
- LTE achieved 23.3 ms onset error and 63.3 ms/beat tempo error when reference
  performances were available.

The LTE result is especially relevant because Rubato can ask Eric for repeated
reference takes of the target excerpt.

## Human Feedback

The paper's qualitative feedback is as important as the metrics:

- Starts and early entrances are stressful because trust is low.
- A purely reactive system can amplify slowdowns.
- A system that follows too much can feel like it is being dragged by the
  soloist.
- The paper identifies a missing fourth task: modeling when to follow and when
  to lead.

## Caveat

The paper evaluates selected solo piano material and demos, not full concerto
conditions. Do not transfer the quantitative results directly to Chopin
orchestral accompaniment without Rubato-specific tests.

## Related

- [ACCompanion](accompanion.md)
- [Accompaniment Control](../concepts/accompaniment-control.md)
