"""CPU-only token, masking and checkpoint-conditioning contract tests."""
import importlib.util
from pathlib import Path
import unittest
import numpy as np

SPEC=importlib.util.spec_from_file_location("check_vhh_generation_conditioning",Path(__file__).resolve().parents[1]/"scripts/check_vhh_generation_conditioning.py")
M=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)


def fixture():
    n=35
    binding=np.zeros(n,dtype=int); binding[:2]=1
    residues=np.zeros((n,33),dtype=int); residues[:,2]=1; residues[0]=0; residues[0,10]=1
    design=np.zeros(n,dtype=bool); design[32:34]=True
    groups=np.r_[np.ones(30),[2,2,0,0,2]].astype(int)
    mask=(groups[:,None]==groups[None,:])&(groups[:,None]>0)
    centres=np.array([[i,i%3,i%5] for i in range(n)],dtype=float)
    return binding,residues,design,groups,mask,centres


class ConditioningTest(unittest.TestCase):
    def test_binding_and_geometry_scope(self):
        result=M.inspect_features(*fixture())
        self.assertEqual(result["binding_token_indices"],[0,1])
        self.assertEqual(result["unspecified_target_token_count"],28)
        self.assertEqual(result["not_binding_token_indices"],[])
        self.assertEqual(result["target_vhh_visible_distance_pair_count"],0)
        self.assertTrue(result["distance_channel_shift_invariant"])

    def test_wrong_epitope_identity_rejected(self):
        data=list(fixture()); data[1][0]=0; data[1][0,8]=1
        with self.assertRaisesRegex(ValueError,"His7/Ala8"): M.inspect_features(*data)

    def test_shifted_binding_indices_rejected(self):
        data=list(fixture()); data[0][:]=0; data[0][2:4]=1
        with self.assertRaisesRegex(ValueError,"His7/Ala8"): M.inspect_features(*data)

    def test_false_cross_group_visibility_rejected(self):
        data=list(fixture()); data[4][0,30]=1
        with self.assertRaisesRegex(ValueError,"distance mask"): M.inspect_features(*data)

    def test_common_group_geometry_is_not_claimed_invariant(self):
        data=list(fixture()); data[3][30:]=1; data[4][:]=True
        result=M.inspect_features(*data)
        self.assertGreater(result["target_vhh_visible_distance_pair_count"],0)
        self.assertFalse(result["distance_channel_shift_invariant"])


if __name__=="__main__": unittest.main()
