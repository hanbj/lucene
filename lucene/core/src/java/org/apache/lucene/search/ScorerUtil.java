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
package org.apache.lucene.search;

import java.io.IOException;
import java.util.stream.LongStream;
import java.util.stream.StreamSupport;
import org.apache.lucene.document.Document;
import org.apache.lucene.document.FeatureField;
import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.ImpactsEnum;
import org.apache.lucene.index.IndexWriter;
import org.apache.lucene.index.IndexWriterConfig;
import org.apache.lucene.index.LeafReader;
import org.apache.lucene.index.PostingsEnum;
import org.apache.lucene.index.TermsEnum;
import org.apache.lucene.store.ByteBuffersDirectory;
import org.apache.lucene.store.Directory;
import org.apache.lucene.util.Bits;
import org.apache.lucene.util.BytesRef;
import org.apache.lucene.util.FixedBitSet;
import org.apache.lucene.util.PriorityQueue;

/** Util class for Scorer related methods */
class ScorerUtil {

  private static final Class<?> DEFAULT_IMPACTS_ENUM_CLASS;

  static {
    try (Directory dir = new ByteBuffersDirectory();
        IndexWriter w = new IndexWriter(dir, new IndexWriterConfig())) {
      Document doc = new Document();
      doc.add(new FeatureField("field", "value", 1f));
      w.addDocument(doc);
      try (DirectoryReader reader = DirectoryReader.open(w)) {
        LeafReader leafReader = reader.leaves().get(0).reader();
        TermsEnum te = leafReader.terms("field").iterator();
        if (te.seekExact(new BytesRef("value")) == false) {
          throw new Error();
        }
        ImpactsEnum ie = te.impacts(PostingsEnum.FREQS);
        DEFAULT_IMPACTS_ENUM_CLASS = ie.getClass();
      }
    } catch (IOException e) {
      throw new Error(e);
    }
  }

  static long costWithMinShouldMatch(LongStream costs, int numScorers, int minShouldMatch) {
    // the idea here is the following: a boolean query c1,c2,...cn with minShouldMatch=m
    // could be rewritten to:
    // (c1 AND (c2..cn|msm=m-1)) OR (!c1 AND (c2..cn|msm=m))
    // if we assume that clauses come in ascending cost, then
    // the cost of the first part is the cost of c1 (because the cost of a conjunction is
    // the cost of the least costly clause)
    // the cost of the second part is the cost of finding m matches among the c2...cn
    // remaining clauses
    // since it is a disjunction overall, the total cost is the sum of the costs of these
    // two parts

    // If we recurse infinitely, we find out that the cost of a msm query is the sum of the
    // costs of the num_scorers - minShouldMatch + 1 least costly scorers
    final PriorityQueue<Long> pq =
        new PriorityQueue<Long>(numScorers - minShouldMatch + 1) {
          @Override
          protected boolean lessThan(Long a, Long b) {
            return a > b;
          }
        };
    costs.forEach(pq::insertWithOverflow);
    return StreamSupport.stream(pq.spliterator(), false).mapToLong(Number::longValue).sum();
  }

  /**
   * Optimize a {@link DocIdSetIterator} for the case when it is likely implemented via an {@link
   * ImpactsEnum}. This return method only has 2 possible return types, which helps make sure that
   * calls to {@link DocIdSetIterator#nextDoc()} and {@link DocIdSetIterator#advance(int)} are
   * bimorphic at most and candidate for inlining.
   */
  static DocIdSetIterator likelyImpactsEnum(DocIdSetIterator it) {
    if (it.getClass() != DEFAULT_IMPACTS_ENUM_CLASS
        && it.getClass() != FilterDocIdSetIterator.class) {
      it = new FilterDocIdSetIterator(it);
    }
    return it;
  }

  /**
   * Optimize a {@link Scorable} for the case when it is likely implemented via a {@link
   * TermScorer}. This return method only has 2 possible return types, which helps make sure that
   * calls to {@link Scorable#score()} are bimorphic at most and candidate for inlining.
   */
  static Scorable likelyTermScorer(Scorable scorable) {
    if (scorable.getClass() != TermScorer.class && scorable.getClass() != FilterScorable.class) {
      scorable = new FilterScorable(scorable);
    }
    return scorable;
  }

  /**
   * Optimize {@link Bits} representing the set of accepted documents for the case when it is likely
   * implemented via a {@link FixedBitSet}. This helps make calls to {@link Bits#get(int)}
   * inlinable, which in-turn helps speed up query evaluation. This is especially helpful as
   * inlining will sometimes enable auto-vectorizing shifts and masks that are done in {@link
   * FixedBitSet#get(int)}.
   */
  static Bits likelyFixedBitSet(Bits acceptDocs) {
    if (acceptDocs instanceof FixedBitSet) {
      return acceptDocs;
    } else if (acceptDocs != null) {
      return new FilterBits(acceptDocs);
    } else {
      return null;
    }
  }

  private static class FilterBits implements Bits {

    private final Bits in;

    FilterBits(Bits in) {
      this.in = in;
    }

    @Override
    public boolean get(int index) {
      return in.get(index);
    }

    @Override
    public int length() {
      return in.length();
    }
  }
}
