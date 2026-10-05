import tempfile
import unittest
import json
from unittest.mock import patch, MagicMock
from pathlib import Path
from openpyxl import Workbook, load_workbook
from classification.classifier import read_rows, validate_batch, export_xlsx, unique_names
from classification.__main__ import main


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'input.xlsx'
        wb = Workbook()
        ws = wb.active
        ws.title = 'Codebook'
        ws.append(['MA', 'TEN', 'Group'])
        ws.append(['001', 'Keo kính', 'Body'])
        ws.append(['002', 'Keo kính', 'Body'])
        ws.append(['003', 'Sơn cửa phải', 'Body'])
        wb.save(self.path)
        wb.close()
        self.rows = read_rows(self.path, 'Codebook')

    def item(self, name):
        return dict(name=name, item_type="service" if name == "Sơn cửa phải" else "part")

    def test_reordered_results_join_by_name_and_preserve_source(self):
        before = self.path.read_bytes()
        items = [self.item('Sơn cửa phải'), self.item('Keo kính')]
        valid = validate_batch({'items': items}, unique_names(self.rows))
        output = self.path.with_name('out.xlsx')
        export_xlsx(self.path, 'Codebook', output, self.rows, {i['name']: i for i in valid['items']})
        wb = load_workbook(output)
        self.assertEqual(wb.active['A2'].value, '001')
        self.assertEqual(wb.active['D2'].value, 'part')
        self.assertEqual(wb.active['D3'].value, 'part')
        self.assertEqual(wb.active['D4'].value, 'service')
        self.assertEqual(wb.active.max_column, 4)
        wb.close()
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_duplicate_unknown_codes_rejected(self):
        for codes in [['Keo kính'], ['Keo kính', 'Keo kính'], ['Keo kính', 'Tên lạ']]:
            with self.assertRaises(ValueError):
                validate_batch({'items': [self.item(c) for c in codes]}, unique_names(self.rows))

    def test_only_two_labels_and_no_extra_fields(self):
        for label in [None, 'material', 'labor', 'paint', 'other', 'unknown']:
            item = self.item('Keo kính')
            item['item_type'] = label
            with self.assertRaises(ValueError):
                validate_batch({'items': [item]}, ['Keo kính'])
        item = self.item('Keo kính')
        item['category'] = 'material'
        with self.assertRaises(ValueError):
            validate_batch({'items': [item]}, ['Keo kính'])

    def test_duplicate_source_code_rejected(self):
        wb = load_workbook(self.path)
        wb.active['A3'] = '001'
        wb.save(self.path)
        wb.close()
        with self.assertRaises(ValueError):
            read_rows(self.path, 'Codebook')

    def test_resume_skips_completed_batch_and_rejects_changed_contract(self):
        run = Path(self.temp.name) / 'run'
        argv = ['classification', '--input', str(self.path), '--sheet', 'Codebook',
                '--run-dir', str(run), '--batch-size', '1', '--model', 'test-model',
                '--env-file', str(Path(self.temp.name) / 'absent.env')]
        def response(code):
            obj = MagicMock()
            obj.status = 'completed'
            obj.output_text = json.dumps({'items': [self.item(code)]})
            obj.model_dump.return_value = {'model': 'test-model'}
            return obj
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'test'}), patch('classification.__main__.genai.Client') as factory:
            create = factory.return_value.__enter__.return_value.interactions.create
            create.return_value = response('Keo kính')
            with patch('sys.argv', argv + ['--max-batches', '1']):
                main()
            self.assertTrue((run / 'classified.partial.xlsx').exists())
            sent = json.loads(create.call_args.kwargs['input'])
            self.assertEqual(sent, {'names': ['Keo kính']})
            partial = json.loads((run / 'summary.json').read_text())
            self.assertEqual(partial['classified_rows'], 2)
            self.assertEqual(partial['classified_unique_names'], 1)
            create.reset_mock()
            create.return_value = response('Sơn cửa phải')
            with patch('sys.argv', argv + ['--resume']):
                main()
            self.assertEqual(create.call_count, 1)
            self.assertTrue((run / 'classified.xlsx').exists())
            self.assertTrue(json.loads((run / 'summary.json').read_text())['complete'])
            with patch('sys.argv', argv + ['--resume', '--batch-size', '2']):
                with self.assertRaises(ValueError):
                    main()


if __name__ == '__main__':
    unittest.main()
