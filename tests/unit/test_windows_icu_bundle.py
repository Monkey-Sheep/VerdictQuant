"""The Windows bundle cannot shadow Qt's OS ICU imports with a tool's ICU."""
import ast
import unittest
from pathlib import Path


class WindowsIcuTests(unittest.TestCase):
    def test_filters_only_root_shims_and_their_data(self):
        spec = Path(__file__).resolve().parents[2] / 'packaging/verdictquant.spec'
        tree = ast.parse(spec.read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'without_shadowing_windows_icu')
        namespace = {'Path': Path}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(spec), 'exec'), namespace)
        entries = [
            ('icuuc.dll', 'tools/poppler/icuuc.dll', 'BINARY'),
            ('icudt78.dll', 'tools/poppler/icudt78.dll', 'BINARY'),
            ('PyQt6/Qt6/bin/Qt6Core.dll', 'qt/Qt6Core.dll', 'BINARY'),
            ('other/icuuc.dll', 'other/icuuc.dll', 'BINARY'),
            ('other/icudt72.dll', 'other/icudt72.dll', 'BINARY'),
        ]
        self.assertEqual(namespace['without_shadowing_windows_icu'](entries), entries[2:])


if __name__ == '__main__':
    unittest.main()
