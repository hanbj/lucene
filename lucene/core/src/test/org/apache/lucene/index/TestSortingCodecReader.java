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

package org.apache.lucene.index;

import com.carrotsearch.randomizedtesting.generators.RandomPicks;
import com.carrotsearch.randomizedtesting.generators.RandomStrings;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import org.apache.lucene.codecs.KnnVectorsReader;
import org.apache.lucene.codecs.TermVectorsReader;
import org.apache.lucene.codecs.hnsw.HnswGraphProvider;
import org.apache.lucene.document.BinaryDocValuesField;
import org.apache.lucene.document.Document;
import org.apache.lucene.document.Field;
import org.apache.lucene.document.FieldType;
import org.apache.lucene.document.KnnFloatVectorField;
import org.apache.lucene.document.LongPoint;
import org.apache.lucene.document.NumericDocValuesField;
import org.apache.lucene.document.SortedDocValuesField;
import org.apache.lucene.document.SortedNumericDocValuesField;
import org.apache.lucene.document.SortedSetDocValuesField;
import org.apache.lucene.document.StringField;
import org.apache.lucene.document.TextField;
import org.apache.lucene.search.DocIdSetIterator;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.Sort;
import org.apache.lucene.search.SortField;
import org.apache.lucene.search.SortedNumericSortField;
import org.apache.lucene.search.SortedSetSelector;
import org.apache.lucene.search.SortedSetSortField;
import org.apache.lucene.search.TermQuery;
import org.apache.lucene.search.TopDocs;
import org.apache.lucene.store.Directory;
import org.apache.lucene.tests.analysis.MockAnalyzer;
import org.apache.lucene.tests.index.RandomIndexWriter;
import org.apache.lucene.tests.util.LuceneTestCase;
import org.apache.lucene.util.BytesRef;
import org.apache.lucene.util.IOUtils;
import org.apache.lucene.util.hnsw.HnswGraph;

public class TestSortingCodecReader extends LuceneTestCase {

  public void testSortOnAddIndicesOrd() throws IOException {
    Directory tmpDir = newDirectory();
    Directory dir = newDirectory();
    IndexWriterConfig iwc = new IndexWriterConfig(new MockAnalyzer(random()));
    IndexWriter w = new IndexWriter(tmpDir, iwc);

    Document doc;
    doc = new Document();
    doc.add(new SortedSetDocValuesField("foo", new BytesRef("b")));
    w.addDocument(doc);

    doc.add(new SortedSetDocValuesField("foo", new BytesRef("a")));
    doc.add(new SortedSetDocValuesField("foo", new BytesRef("b")));
    doc.add(new SortedSetDocValuesField("foo", new BytesRef("b")));
    w.addDocument(doc);

    w.commit();

    Sort indexSort = new Sort(new SortedSetSortField("foo", false, SortedSetSelector.Type.MIN));
    try (DirectoryReader reader = DirectoryReader.open(tmpDir)) {
      for (LeafReaderContext ctx : reader.leaves()) {
        CodecReader wrap =
            SortingCodecReader.wrap(SlowCodecReaderWrapper.wrap(ctx.reader()), indexSort);
        assertTrue(wrap.toString(), wrap.toString().startsWith("SortingCodecReader("));
        SortingCodecReader sortingCodecReader = (SortingCodecReader) wrap;
        SortedSetDocValues sortedSetDocValues =
            sortingCodecReader
                .getDocValuesReader()
                .getSortedSet(ctx.reader().getFieldInfos().fieldInfo("foo"));
        sortedSetDocValues.nextDoc();
        assertEquals(sortedSetDocValues.docValueCount(), 2);
        sortedSetDocValues.nextDoc();
        assertEquals(sortedSetDocValues.docValueCount(), 1);
        assertEquals(sortedSetDocValues.nextDoc(), DocIdSetIterator.NO_MORE_DOCS);
      }
    }
    IOUtils.close(w, dir, tmpDir);
  }

  public void testSortOnAddIndicesInt() throws IOException {
    Directory tmpDir = newDirectory();
    Directory dir = newDirectory();
    IndexWriterConfig iwc = new IndexWriterConfig(new MockAnalyzer(random()));
    IndexWriter w = new IndexWriter(tmpDir, iwc);
    Document doc = new Document();
    doc.add(new NumericDocValuesField("foo", 18));
    w.addDocument(doc);

    doc = new Document();
    doc.add(new NumericDocValuesField("foo", -1));
    w.addDocument(doc);
    w.commit();

    doc = new Document();
    doc.add(new NumericDocValuesField("foo", 7));
    w.addDocument(doc);
    w.commit();
    w.close();
    Sort indexSort = new Sort(new SortField("foo", SortField.Type.INT));

    iwc = new IndexWriterConfig(new MockAnalyzer(random())).setIndexSort(indexSort);
    w = new IndexWriter(dir, iwc);
    try (DirectoryReader reader = DirectoryReader.open(tmpDir)) {
      List<CodecReader> readers = new ArrayList<>();
      for (LeafReaderContext ctx : reader.leaves()) {
        CodecReader wrap =
            SortingCodecReader.wrap(SlowCodecReaderWrapper.wrap(ctx.reader()), indexSort);
        assertTrue(wrap.toString(), wrap.toString().startsWith("SortingCodecReader("));
        readers.add(wrap);
      }
      w.addIndexes(readers.toArray(new CodecReader[0]));
    }
    DirectoryReader r = DirectoryReader.open(w);
    LeafReader leaf = getOnlyLeafReader(r);
    assertEquals(3, leaf.maxDoc());
    NumericDocValues values = leaf.getNumericDocValues("foo");
    assertEquals(0, values.nextDoc());
    assertEquals(-1, values.longValue());
    assertEquals(1, values.nextDoc());
    assertEquals(7, values.longValue());
    assertEquals(2, values.nextDoc());
    assertEquals(18, values.longValue());
    assertNotNull(leaf.getMetaData().sort());
    IOUtils.close(r, w, dir, tmpDir);
  }

  public void testSortOnAddIndicesRandom() throws IOException {
    try (Directory dir = newDirectory()) {
      int numDocs = atLeast(200);
      int actualNumDocs;
      List<Integer> docIds = new ArrayList<>(numDocs);
      for (int i = 0; i < numDocs; i++) {
        docIds.add(i);
      }
      Collections.shuffle(docIds, random());
      // If true, index a vector and points for every doc
      boolean dense = random().nextBoolean();
      try (RandomIndexWriter iw = new RandomIndexWriter(random(), dir)) {
        for (int i = 0; i < numDocs; i++) {
          int docId = docIds.get(i);
          Document doc = new Document();
          doc.add(new StringField("string_id", Integer.toString(docId), Field.Store.YES));
          if (dense || docId % 3 == 0) {
            doc.add(new LongPoint("point_id", docId));
          }
          String s = RandomStrings.randomRealisticUnicodeOfLength(random(), 25);
          doc.add(new TextField("text_field", s, Field.Store.YES));
          doc.add(new BinaryDocValuesField("text_field", new BytesRef(s)));
          doc.add(new TextField("another_text_field", s, Field.Store.YES));
          doc.add(new BinaryDocValuesField("another_text_field", new BytesRef(s)));
          doc.add(new SortedNumericDocValuesField("sorted_numeric_dv", docId));
          doc.add(
              new SortedDocValuesField("binary_sorted_dv", new BytesRef(Integer.toString(docId))));
          doc.add(new BinaryDocValuesField("binary_dv", new BytesRef(Integer.toString(docId))));
          doc.add(
              new SortedSetDocValuesField("sorted_set_dv", new BytesRef(Integer.toString(docId))));
          if (dense || docId % 2 == 0) {
            doc.add(new KnnFloatVectorField("vector", new float[] {(float) docId}));
          }
          doc.add(new NumericDocValuesField("foo", random().nextInt(20)));

          FieldType ft = new FieldType(StringField.TYPE_NOT_STORED);
          ft.setStoreTermVectors(true);
          doc.add(new Field("term_vectors", "test" + docId, ft));
          if (rarely() == false) {
            doc.add(new NumericDocValuesField("id", docId));
            doc.add(
                new SortedSetDocValuesField(
                    "sorted_set_sort_field",
                    new BytesRef(String.format(Locale.ROOT, "%06d", docId))));
            doc.add(
                new SortedDocValuesField(
                    "sorted_binary_sort_field",
                    new BytesRef(String.format(Locale.ROOT, "%06d", docId))));
            doc.add(new SortedNumericDocValuesField("sorted_numeric_sort_field", docId));
          } else {
            doc.add(new NumericDocValuesField("alt_id", docId));
          }
          iw.addDocument(doc);
          if (i > 0 && random().nextInt(5) == 0) {
            final int id = RandomPicks.randomFrom(random(), docIds.subList(0, i));
            iw.deleteDocuments(new Term("string_id", Integer.toString(id)));
          }
        }
        iw.commit();
        actualNumDocs = iw.getDocStats().numDocs;
      }
      Sort indexSort =
          RandomPicks.randomFrom(
              random(),
              Arrays.asList(
                  new Sort(
                      new SortField("id", SortField.Type.INT),
                      new SortField("alt_id", SortField.Type.INT)),
                  new Sort(
                      new SortedSetSortField("sorted_set_sort_field", false),
                      new SortField("alt_id", SortField.Type.INT)),
                  new Sort(
                      new SortedNumericSortField("sorted_numeric_sort_field", SortField.Type.INT),
                      new SortField("alt_id", SortField.Type.INT)),
                  new Sort(
                      new SortField("sorted_binary_sort_field", SortField.Type.STRING, false),
                      new SortField("alt_id", SortField.Type.INT))));
      try (Directory sortDir = newDirectory()) {
        try (IndexWriter writer =
            new IndexWriter(sortDir, newIndexWriterConfig().setIndexSort(indexSort))) {
          try (DirectoryReader reader = DirectoryReader.open(dir)) {
            List<CodecReader> readers = new ArrayList<>();
            for (LeafReaderContext ctx : reader.leaves()) {
              CodecReader wrap =
                  SortingCodecReader.wrap(SlowCodecReaderWrapper.wrap(ctx.reader()), indexSort);
              readers.add(wrap);
              TermVectorsReader termVectorsReader = wrap.getTermVectorsReader();
              TermVectorsReader clone = termVectorsReader.clone();
              assertNotSame(termVectorsReader, clone);
              clone.close();
            }
            writer.addIndexes(readers.toArray(new CodecReader[0]));
          }
          assumeTrue("must have at least one doc", actualNumDocs > 0);
          try (DirectoryReader r = DirectoryReader.open(writer)) {
            LeafReader leaf = getOnlyLeafReader(r);
            assertEquals(actualNumDocs, leaf.maxDoc());
            BinaryDocValues binary_dv = leaf.getBinaryDocValues("binary_dv");
            SortedNumericDocValues sorted_numeric_dv =
                leaf.getSortedNumericDocValues("sorted_numeric_dv");
            SortedSetDocValues sorted_set_dv = leaf.getSortedSetDocValues("sorted_set_dv");
            SortedDocValues binary_sorted_dv = leaf.getSortedDocValues("binary_sorted_dv");
            FloatVectorValues vectorValues = leaf.getFloatVectorValues("vector");
            KnnVectorsReader vectorsReader = ((CodecReader) leaf).getVectorReader();
            HnswGraph graph;
            if (vectorsReader instanceof HnswGraphProvider hnswGraphProvider) {
              graph = hnswGraphProvider.getGraph("vector");
            } else {
              graph = null;
            }
            NumericDocValues ids = leaf.getNumericDocValues("id");
            long prevValue = -1;
            boolean usingAltIds = false;
            KnnVectorValues.DocIndexIterator valuesIterator = vectorValues.iterator();
            for (int i = 0; i < actualNumDocs; i++) {
              int idNext = ids.nextDoc();
              if (idNext == DocIdSetIterator.NO_MORE_DOCS) {
                assertFalse(usingAltIds);
                usingAltIds = true;
                ids = leaf.getNumericDocValues("alt_id");
                idNext = ids.nextDoc();
                binary_dv = leaf.getBinaryDocValues("binary_dv");
                sorted_numeric_dv = leaf.getSortedNumericDocValues("sorted_numeric_dv");
                sorted_set_dv = leaf.getSortedSetDocValues("sorted_set_dv");
                binary_sorted_dv = leaf.getSortedDocValues("binary_sorted_dv");
                vectorValues = leaf.getFloatVectorValues("vector");
                valuesIterator = vectorValues.iterator();
                prevValue = -1;
              }
              assertTrue(prevValue + " < " + ids.longValue(), prevValue < ids.longValue());
              prevValue = ids.longValue();
              assertTrue(binary_dv.advanceExact(idNext));
              assertTrue(sorted_numeric_dv.advanceExact(idNext));
              assertTrue(sorted_set_dv.advanceExact(idNext));
              assertTrue(binary_sorted_dv.advanceExact(idNext));
              if (dense || prevValue % 2 == 0) {
                assertEquals(idNext, valuesIterator.advance(idNext));
                if (graph != null) {
                  graph.seek(0, valuesIterator.index());
                  assertNotEquals(DocIdSetIterator.NO_MORE_DOCS, graph.nextNeighbor());
                }
              }

              assertEquals(new BytesRef(ids.longValue() + ""), binary_dv.binaryValue());
              assertEquals(
                  new BytesRef(ids.longValue() + ""),
                  binary_sorted_dv.lookupOrd(binary_sorted_dv.ordValue()));
              assertEquals(
                  new BytesRef(ids.longValue() + ""),
                  sorted_set_dv.lookupOrd(sorted_set_dv.nextOrd()));
              assertEquals(1, sorted_set_dv.docValueCount());
              assertEquals(1, sorted_numeric_dv.docValueCount());
              assertEquals(ids.longValue(), sorted_numeric_dv.nextValue());

              if (dense || prevValue % 2 == 0) {
                float[] vectorValue = vectorValues.vectorValue(valuesIterator.index());
                assertEquals(1, vectorValue.length);
                assertEquals((float) ids.longValue(), vectorValue[0], 0.001f);
              }

              Fields termVectors = leaf.termVectors().get(idNext);
              assertTrue(
                  termVectors
                      .terms("term_vectors")
                      .iterator()
                      .seekExact(new BytesRef("test" + ids.longValue())));
              assertEquals(
                  Long.toString(ids.longValue()),
                  leaf.storedFields().document(idNext).get("string_id"));
              IndexSearcher searcher = new IndexSearcher(r);
              TopDocs result =
                  searcher.search(LongPoint.newExactQuery("point_id", ids.longValue()), 10);
              if (dense || ids.longValue() % 3 == 0) {
                assertEquals(1, result.totalHits.value());
                assertEquals(idNext, result.scoreDocs[0].doc);
              } else {
                assertEquals(0, result.totalHits.value());
              }

              result =
                  searcher.search(new TermQuery(new Term("string_id", "" + ids.longValue())), 1);
              assertEquals(1, result.totalHits.value());
              assertEquals(idNext, result.scoreDocs[0].doc);
            }
            assertEquals(DocIdSetIterator.NO_MORE_DOCS, ids.nextDoc());
          }
        }
      }
    }
  }
}
