class Product < ApplicationRecord
  has_many :orders

  validates :name, presence: true
  validates :price_cents, numericality: { only_integer: true, greater_than_or_equal_to: 0 }
end
