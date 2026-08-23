from __future__ import annotations

import unittest

import numpy as np

try:
    import torch
except ModuleNotFoundError as error:
    if error.name != "torch":
        raise
    torch = None

if torch is not None:
    from pyresearch.event.deep_model import CausalTCN, make_sequences, multitask_loss, parameter_count


@unittest.skipUnless(torch is not None, "optional TCN tests require torch")
class EventTcnTest(unittest.TestCase):
    def test_sequence_padding_never_crosses_segment(self) -> None:
        values = np.arange(12, dtype="float32").reshape(6, 2)
        starts = np.array([0, 0, 0, 3, 3, 3], dtype="int64")
        sequence = make_sequences(values, np.array([3]), starts, 128).numpy()[0]
        np.testing.assert_array_equal(sequence[:-1], np.zeros((6, 2), dtype="float32"))
        np.testing.assert_array_equal(sequence[-1], values[3])

    def test_causal_tcn_ignores_history_outside_receptive_field(self) -> None:
        torch.manual_seed(7)
        model = CausalTCN(3, [4, 4], 3, [1, 2], 0.0).eval()
        left = torch.randn(2, 128, 3)
        right = left.clone()
        right[:, :-7, :] = torch.randn_like(right[:, :-7, :])
        with torch.no_grad():
            left_output = model(left)
            right_output = model(right)
        torch.testing.assert_close(left_output[0], right_output[0])
        torch.testing.assert_close(left_output[1], right_output[1])

    def test_frozen_tcn_parameter_count(self) -> None:
        model = CausalTCN(24, [32, 32], 3, [1, 2], 0.1)
        self.assertEqual(parameter_count(model), 5506)

    def test_deep_markout_loss_is_filled_valid_only(self) -> None:
        logits = torch.tensor([0.0, 0.0])
        predictions = torch.tensor([1.0, 2.0])
        targets = torch.tensor([3.0, 9999.0])
        valid = torch.tensor([True, False])
        first = multitask_loss(logits, predictions, torch.tensor([1.0, 0.0]), targets, valid)
        targets[1] = -9999.0
        second = multitask_loss(logits, predictions, torch.tensor([1.0, 0.0]), targets, valid)
        torch.testing.assert_close(first, second)


if __name__ == "__main__":
    unittest.main()
