/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.lucene.util;

import static org.hamcrest.Matchers.equalTo;
import static org.hamcrest.Matchers.greaterThanOrEqualTo;
import static org.hamcrest.Matchers.lessThanOrEqualTo;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.List;
import java.util.NoSuchElementException;
import java.util.Random;
import org.apache.lucene.tests.util.LuceneTestCase;
import org.apache.lucene.tests.util.TestUtil;
import org.hamcrest.Matchers;

@SuppressWarnings("BoxedPrimitiveEquality")
public class TestPriorityQueue extends LuceneTestCase {

  private static class IntegerQueue extends PriorityQueue<Integer> {
    public IntegerQueue(int count) {
      super(count, (a, b) -> a < b);
    }

    protected final void checkValidity() {
      Object[] heapArray = getHeapArray();
      for (int i = 1; i <= size(); i++) {
        int parent = i >>> 1;
        if (parent > 1) {
          assertThat((Integer) heapArray[i], greaterThanOrEqualTo((Integer) heapArray[parent]));
        }
      }
    }
  }

  public void testZeroSizedQueue() {
    PriorityQueue<Integer> pq = new IntegerQueue(0);
    assertEquals((Object) 1, pq.insertWithOverflow(1));
    assertEquals(0, pq.size());

    // should fail, but passes and modifies the top...
    pq.add(1);
    assertEquals((Object) 1, pq.top());
  }

  public void testNoExtraWorkOnEqualElements() {
    class Value {
      private final int index;
      private final int value;

      Value(int index, int value) {
        this.index = index;
        this.value = value;
      }
    }

    PriorityQueue<Value> pq = new PriorityQueue<>(5, (a, b) -> a.value < b.value);

    // Make all elements equal but record insertion order.
    for (int i = 0; i < 100; i++) {
      pq.insertWithOverflow(new Value(i, 0));
    }

    ArrayList<Integer> indexes = new ArrayList<>();
    for (Value e : pq) {
      indexes.add(e.index);
    }

    // All elements are "equal" so we should have exactly the indexes of those elements that were
    // added first.
    assertThat(indexes, Matchers.containsInAnyOrder(0, 1, 2, 3, 4));
  }

  public void testPQ() throws Exception {
    int size = atLeast(10000);
    testPQ(new IntegerQueue(size), size, random());
  }

  public void testComparatorPQ() throws Exception {
    int size = atLeast(10000);
    testPQ(PriorityQueue.usingComparator(size, Integer::compareTo), size, random());
  }

  public static void testPQ(PriorityQueue<Integer> pq, int count, Random gen) {
    int sum = 0, sum2 = 0;

    for (int i = 0; i < count; i++) {
      int next = gen.nextInt();
      sum += next;
      pq.add(next);
    }

    int last = Integer.MIN_VALUE;
    for (int i = 0; i < count; i++) {
      Integer next = pq.pop();
      assertThat(next, greaterThanOrEqualTo(last));
      last = next.intValue();
      sum2 += last;
    }

    assertEquals(sum, sum2);
  }

  public void testClear() {
    PriorityQueue<Integer> pq = new IntegerQueue(3);
    pq.add(2);
    pq.add(3);
    pq.add(1);
    assertEquals(3, pq.size());
    pq.clear();
    assertEquals(0, pq.size());
  }

  public void testFixedSize() {
    PriorityQueue<Integer> pq = new IntegerQueue(3);
    pq.insertWithOverflow(2);
    pq.insertWithOverflow(3);
    pq.insertWithOverflow(1);
    pq.insertWithOverflow(5);
    pq.insertWithOverflow(7);
    pq.insertWithOverflow(1);
    assertEquals(3, pq.size());
    assertEquals((Integer) 3, pq.top());
  }

  public void testInsertWithOverflow() {
    int size = 4;
    PriorityQueue<Integer> pq = new IntegerQueue(size);
    Integer i1 = 2;
    Integer i2 = 3;
    Integer i3 = 1;
    Integer i4 = 5;
    Integer i5 = 7;
    Integer i6 = 1;

    assertNull(pq.insertWithOverflow(i1));
    assertNull(pq.insertWithOverflow(i2));
    assertNull(pq.insertWithOverflow(i3));
    assertNull(pq.insertWithOverflow(i4));
    assertThat(pq.insertWithOverflow(i5), equalTo(i3)); // i3 should have been dropped
    assertThat(pq.insertWithOverflow(i6), equalTo(i6)); // i6 should not have been inserted
    assertThat(pq.size(), equalTo(size));
    assertThat(pq.top(), equalTo(2));
  }

  public void testAddAllToEmptyQueue() {
    Random random = random();
    int size = 10;
    List<Integer> list = new ArrayList<>();
    for (int i = 0; i < size; i++) {
      list.add(random.nextInt());
    }
    IntegerQueue pq = new IntegerQueue(size);
    pq.addAll(list);

    pq.checkValidity();
    assertOrderedWhenDrained(pq, list);
  }

  public void testAddAllToPartiallyFilledQueue() {
    IntegerQueue pq = new IntegerQueue(20);
    List<Integer> oneByOne = new ArrayList<>();
    List<Integer> bulkAdded = new ArrayList<>();
    Random random = random();
    for (int i = 0; i < 10; i++) {
      bulkAdded.add(random.nextInt());

      int x = random.nextInt();
      pq.add(x);
      oneByOne.add(x);
    }

    pq.addAll(bulkAdded);
    pq.checkValidity();

    oneByOne.addAll(bulkAdded); // Gather all "reference" data.
    assertOrderedWhenDrained(pq, oneByOne);
  }

  public void testAddAllDoesNotFitIntoQueue() {
    IntegerQueue pq = new IntegerQueue(20);
    List<Integer> list = new ArrayList<>();
    Random random = random();
    for (int i = 0; i < 11; i++) {
      list.add(random.nextInt());
      pq.add(random.nextInt());
    }

    assertThrows(
        "Cannot add 11 elements to a queue with remaining capacity: 9",
        ArrayIndexOutOfBoundsException.class,
        () -> pq.addAll(list));
  }

  public void testRemovalsAndInsertions() {
    Random random = random();
    int numDocsInPQ = TestUtil.nextInt(random, 1, 100);
    IntegerQueue pq = new IntegerQueue(numDocsInPQ);
    Integer lastLeast = null;

    // Basic insertion of new content
    ArrayList<Integer> sds = new ArrayList<>(numDocsInPQ);
    for (int i = 0; i < numDocsInPQ * 10; i++) {
      Integer newEntry = Math.abs(random.nextInt());
      sds.add(newEntry);
      Integer evicted = pq.insertWithOverflow(newEntry);
      pq.checkValidity();
      if (evicted != null) {
        assertTrue(sds.remove(evicted));
        if (evicted != newEntry) {
          assertSame(evicted, lastLeast);
        }
      }
      Integer newLeast = pq.top();
      if ((lastLeast != null) && (newLeast != newEntry) && (newLeast != lastLeast)) {
        // If there has been a change of least entry and it wasn't our new
        // addition we expect the scores to increase
        assertThat(newLeast, lessThanOrEqualTo(newEntry));
        assertThat(newLeast, greaterThanOrEqualTo(lastLeast));
      }
      lastLeast = newLeast;
    }

    // Try many random additions to existing entries - we should always see
    // increasing scores in the lowest entry in the PQ
    for (int p = 0; p < 500000; p++) {
      int element = (int) (random.nextFloat() * (sds.size() - 1));
      Integer objectToRemove = sds.get(element);
      assertSame(sds.remove(element), objectToRemove);
      assertTrue(pq.remove(objectToRemove));
      pq.checkValidity();
      Integer newEntry = Math.abs(random.nextInt());
      sds.add(newEntry);
      assertNull(pq.insertWithOverflow(newEntry));
      pq.checkValidity();
      Integer newLeast = pq.top();
      if ((objectToRemove != lastLeast) && (lastLeast != null) && (newLeast != newEntry)) {
        // If there has been a change of least entry and it wasn't our new
        // addition or the loss of our randomly removed entry we expect the
        // scores to increase
        assertThat(newLeast, lessThanOrEqualTo(newEntry));
        assertThat(newLeast, greaterThanOrEqualTo(lastLeast));
      }
      lastLeast = newLeast;
    }
  }

  public void testIteratorEmpty() {
    IntegerQueue queue = new IntegerQueue(3);

    Iterator<Integer> it = queue.iterator();
    assertFalse(it.hasNext());
    expectThrows(
        NoSuchElementException.class,
        () -> {
          it.next();
        });
  }

  public void testIteratorOne() {
    IntegerQueue queue = new IntegerQueue(3);

    queue.add(1);
    Iterator<Integer> it = queue.iterator();
    assertTrue(it.hasNext());
    assertEquals(Integer.valueOf(1), it.next());
    assertFalse(it.hasNext());
    expectThrows(
        NoSuchElementException.class,
        () -> {
          it.next();
        });
  }

  public void testIteratorTwo() {
    IntegerQueue queue = new IntegerQueue(3);

    queue.add(1);
    queue.add(2);
    Iterator<Integer> it = queue.iterator();
    assertTrue(it.hasNext());
    assertEquals(Integer.valueOf(1), it.next());
    assertTrue(it.hasNext());
    assertEquals(Integer.valueOf(2), it.next());
    assertFalse(it.hasNext());
    expectThrows(
        NoSuchElementException.class,
        () -> {
          it.next();
        });
  }

  public void testIteratorRandom() {
    final int maxSize = TestUtil.nextInt(random(), 1, 20);
    IntegerQueue queue = new IntegerQueue(maxSize);
    final int iters = atLeast(100);
    final List<Integer> expected = new ArrayList<>();
    for (int iter = 0; iter < iters; ++iter) {
      if (queue.size() == 0 || (queue.size() < maxSize && random().nextBoolean())) {
        final Integer value = random().nextInt(10);
        queue.add(value);
        expected.add(value);
      } else {
        expected.remove(queue.pop());
      }
      List<Integer> actual = new ArrayList<>();
      for (Integer value : queue) {
        actual.add(value);
      }
      CollectionUtil.introSort(expected);
      CollectionUtil.introSort(actual);
      assertEquals(expected, actual);
    }
  }

  public void testMaxIntSize() {
    expectThrows(
        IllegalArgumentException.class,
        () -> new PriorityQueue<Boolean>(Integer.MAX_VALUE, (_, _) -> true));
  }

  private void assertOrderedWhenDrained(IntegerQueue pq, List<Integer> referenceDataList) {
    Collections.sort(referenceDataList);
    int i = 0;
    while (pq.size() > 0) {
      assertEquals(pq.pop(), referenceDataList.get(i));
      i++;
    }
  }
}
