class Account < ApplicationRecord
  belongs_to :owner, class_name: "User"
  has_many :users, foreign_key: :account_id

  validates :name, presence: true
  validates :plan, inclusion: { in: %w[free pro enterprise] }
end
